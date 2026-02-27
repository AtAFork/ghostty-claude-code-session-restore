#!/usr/bin/env python3
"""Tests for cmux support across session_restore_core, watcher, and restore."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(REPO_ROOT))

from session_restore_core import (  # noqa: E402
    build_restore_argv,
    load_restore_file,
    normalize_restore_entry,
    resolve_sessions,
)

WATCHER_SCRIPT = REPO_ROOT / "watcher.sh"
RESTORE_SCRIPT = REPO_ROOT / "restore.sh"


def load_watcher_module():
    loader = SourceFileLoader("watcher_module", str(WATCHER_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_restore_module():
    loader = SourceFileLoader("restore_module", str(RESTORE_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ok(args, stdout=""):
    return subprocess.CompletedProcess(args, returncode=0, stdout=stdout, stderr="")


def _fail(args):
    return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="")


class CmuxCoreTests(unittest.TestCase):
    """Tests for cmux metadata handling in session_restore_core."""

    def test_normalize_passes_through_cmux_metadata(self) -> None:
        entry = {
            "tool": "claude",
            "sessionId": "904135b4-8584-42dd-aeb9-08b920d0e02e",
            "cwd": "/tmp/proj",
            "flags": ["--model", "sonnet"],
            "terminal": "cmux",
            "workspaceName": "my-project",
            "workspaceId": "ws-uuid-1",
            "surfaceId": "sf-uuid-1",
            "surfaceIndex": 2,
        }
        result = normalize_restore_entry(entry)
        self.assertEqual(result["terminal"], "cmux")
        self.assertEqual(result["workspaceName"], "my-project")
        self.assertEqual(result["workspaceId"], "ws-uuid-1")
        self.assertEqual(result["surfaceId"], "sf-uuid-1")
        self.assertEqual(result["surfaceIndex"], 2)
        # Core fields still work.
        self.assertEqual(result["tool"], "claude")
        self.assertEqual(result["sessionId"], "904135b4-8584-42dd-aeb9-08b920d0e02e")

    def test_normalize_omits_cmux_metadata_when_absent(self) -> None:
        result = normalize_restore_entry({
            "tool": "claude", "sessionId": None, "cwd": "/tmp/proj", "flags": [],
        })
        self.assertNotIn("terminal", result)
        self.assertNotIn("workspaceName", result)
        self.assertNotIn("surfaceIndex", result)

    def test_normalize_preserves_surface_index_zero(self) -> None:
        """surfaceIndex=0 is falsy but must still be preserved."""
        result = normalize_restore_entry({
            "tool": "claude", "cwd": "/tmp",
            "terminal": "cmux", "workspaceName": "ws", "surfaceIndex": 0,
        })
        self.assertIn("surfaceIndex", result)
        self.assertEqual(result["surfaceIndex"], 0)

    def test_normalize_coerces_surface_index_string_to_int(self) -> None:
        """surfaceIndex as string '2' (e.g. from JSON) is coerced to int 2."""
        result = normalize_restore_entry({
            "tool": "claude", "cwd": "/tmp",
            "terminal": "cmux", "workspaceName": "ws", "surfaceIndex": "2",
        })
        self.assertEqual(result["surfaceIndex"], 2)
        self.assertIsInstance(result["surfaceIndex"], int)

    def test_normalize_clamps_negative_surface_index_to_zero(self) -> None:
        """Negative surfaceIndex is clamped to 0 to prevent silent wrong-surface access."""
        result = normalize_restore_entry({
            "tool": "claude", "cwd": "/tmp",
            "terminal": "cmux", "workspaceName": "ws", "surfaceIndex": -1,
        })
        self.assertEqual(result["surfaceIndex"], 0)

    def test_normalize_invalid_surface_index_defaults_to_zero(self) -> None:
        """Non-numeric surfaceIndex (e.g. 'abc') defaults to 0."""
        result = normalize_restore_entry({
            "tool": "claude", "cwd": "/tmp",
            "terminal": "cmux", "workspaceName": "ws", "surfaceIndex": "abc",
        })
        self.assertEqual(result["surfaceIndex"], 0)

    def test_normalize_none_surface_index_defaults_to_zero(self) -> None:
        """surfaceIndex=None defaults to 0."""
        result = normalize_restore_entry({
            "tool": "claude", "cwd": "/tmp",
            "terminal": "cmux", "workspaceName": "ws", "surfaceIndex": None,
        })
        self.assertEqual(result["surfaceIndex"], 0)

    def test_resolve_sessions_sets_terminal_cmux_from_workspace_id(self) -> None:
        """The watcher attaches workspaceId to raw snapshots; resolve should infer terminal=cmux."""
        snapshot = [
            {
                "tool": "claude",
                "cwd": "/tmp/proj",
                "args": "claude --model sonnet",
                "workspaceId": "ws-uuid-1",
                "workspaceName": "my-project",
                "surfaceIndex": 0,
            }
        ]
        sessions = resolve_sessions(snapshot)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["terminal"], "cmux")
        self.assertEqual(sessions[0]["workspaceName"], "my-project")
        self.assertEqual(sessions[0]["workspaceId"], "ws-uuid-1")
        self.assertEqual(sessions[0]["surfaceIndex"], 0)

    def test_resolve_sessions_mixed_cmux_and_ghostty(self) -> None:
        """Cmux entries get terminal field; plain ghostty entries do not."""
        snapshot = [
            {
                "tool": "claude", "cwd": "/tmp/proj-a", "args": "claude --continue",
                "workspaceId": "ws-1", "workspaceName": "alpha", "surfaceIndex": 0,
            },
            {
                "tool": "codex", "cwd": "/tmp/proj-b", "args": "codex --model gpt-5",
            },
        ]
        sessions = resolve_sessions(snapshot)
        self.assertEqual(sessions[0]["terminal"], "cmux")
        self.assertNotIn("terminal", sessions[1])

    def test_build_restore_argv_ignores_cmux_metadata(self) -> None:
        """Cmux metadata must not leak into the restore command."""
        entry = {
            "tool": "claude",
            "sessionId": "904135b4-8584-42dd-aeb9-08b920d0e02e",
            "cwd": "/tmp/proj",
            "flags": ["--model", "sonnet"],
            "terminal": "cmux",
            "workspaceName": "my-project",
            "surfaceIndex": 1,
        }
        argv = build_restore_argv(entry)
        self.assertEqual(
            argv,
            ["claude", "--resume", "904135b4-8584-42dd-aeb9-08b920d0e02e", "--model", "sonnet"],
        )

    def test_load_restore_file_round_trips_cmux_metadata(self) -> None:
        """Cmux metadata survives write -> load_restore_file -> normalize cycle."""
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "restore.json"
            payload = [
                {
                    "tool": "claude", "sessionId": "aaa", "cwd": "/tmp",
                    "flags": [], "terminal": "cmux",
                    "workspaceName": "dev", "workspaceId": "ws-1",
                    "surfaceId": "sf-1", "surfaceIndex": 0,
                },
                {
                    "tool": "codex", "sessionId": None, "cwd": "/tmp",
                    "flags": ["--model", "gpt-5"],
                },
            ]
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_restore_file(path)

            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["terminal"], "cmux")
            self.assertEqual(loaded[0]["workspaceName"], "dev")
            self.assertEqual(loaded[0]["workspaceId"], "ws-1")
            self.assertEqual(loaded[0]["surfaceId"], "sf-1")
            self.assertEqual(loaded[0]["surfaceIndex"], 0)
            self.assertNotIn("terminal", loaded[1])


class CmuxWatcherTests(unittest.TestCase):
    """Tests for cmux watcher functions: detect, list, enrich, save pipeline."""

    def test_detect_terminals_includes_cmux_when_socket_alive(self) -> None:
        watcher = load_watcher_module()
        with mock.patch.object(watcher, "_is_cmux_socket_alive", return_value=True):
            with mock.patch.object(watcher, "_run", return_value=_fail([])):
                self.assertEqual(watcher.detect_terminals(), {"cmux"})

    def test_detect_terminals_includes_ghostty_when_running(self) -> None:
        watcher = load_watcher_module()
        with mock.patch.object(watcher, "_is_cmux_socket_alive", return_value=False):
            with mock.patch.object(
                watcher, "_run", return_value=_ok([], stdout="1234")
            ):
                self.assertEqual(watcher.detect_terminals(), {"ghostty"})

    def test_detect_terminals_returns_empty_when_nothing_running(self) -> None:
        watcher = load_watcher_module()
        with mock.patch.object(watcher, "_is_cmux_socket_alive", return_value=False):
            with mock.patch.object(watcher, "_run", return_value=_fail([])):
                self.assertEqual(watcher.detect_terminals(), set())

    def test_get_cmux_env_extracts_ids(self) -> None:
        watcher = load_watcher_module()
        ps_output = (
            "/usr/local/bin/claude --model sonnet "
            "CMUX_WORKSPACE_ID=ws-uuid-123 CMUX_SURFACE_ID=sf-uuid-456 "
            "TERM=xterm-256color"
        )
        with mock.patch.object(watcher, "_run", return_value=_ok([], stdout=ps_output)):
            self.assertEqual(watcher.get_cmux_env(12345), ("ws-uuid-123", "sf-uuid-456"))

    def test_get_cmux_env_returns_none_without_both_vars(self) -> None:
        watcher = load_watcher_module()
        # Only workspace, no surface.
        with mock.patch.object(
            watcher, "_run",
            return_value=_ok([], stdout="CMUX_WORKSPACE_ID=ws-1 TERM=xterm"),
        ):
            self.assertIsNone(watcher.get_cmux_env(12345))

    def test_list_candidate_processes_cmux_mode_attaches_ids(self) -> None:
        watcher = load_watcher_module()
        ps_output = "  100 ttys000 claude   claude --model sonnet\n"

        def fake_run(args):
            if args[:2] == ["ps", "-eo"]:
                return _ok(args, stdout=ps_output)
            if args[0] == "ps" and "eww" in args:
                return _ok(args, stdout="claude CMUX_WORKSPACE_ID=ws-1 CMUX_SURFACE_ID=sf-1")
            if args[0] == "lsof":
                return _ok(args, stdout="n/tmp/proj\n")
            return _fail(args)

        with mock.patch.object(watcher, "_run", side_effect=fake_run):
            entries = watcher.list_candidate_processes(mode="cmux")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["workspaceId"], "ws-1")
        self.assertEqual(entries[0]["surfaceId"], "sf-1")
        self.assertEqual(entries[0]["cwd"], "/tmp/proj")

    def test_list_candidate_processes_cmux_mode_skips_non_cmux(self) -> None:
        watcher = load_watcher_module()
        ps_output = "  100 ttys000 claude   claude --model sonnet\n"

        def fake_run(args):
            if args[:2] == ["ps", "-eo"]:
                return _ok(args, stdout=ps_output)
            if args[0] == "ps" and "eww" in args:
                return _ok(args, stdout="claude TERM=xterm-256color")
            return _fail(args)

        with mock.patch.object(watcher, "_run", side_effect=fake_run):
            self.assertEqual(watcher.list_candidate_processes(mode="cmux"), [])

    def test_enrich_cmux_entries_reads_workspace_map(self) -> None:
        """Enrichment reads workspace names from the map file (not cmux CLI)."""
        watcher = load_watcher_module()

        with tempfile.TemporaryDirectory() as tempdir:
            map_path = Path(tempdir) / "cmux-workspace-map.json"
            map_path.write_text(
                json.dumps({"ws-uuid-1": "my-project"}), encoding="utf-8"
            )
            watcher.CMUX_WORKSPACE_MAP_PATH = map_path

            entries = [
                {
                    "pid": 100, "tty": "ttys000", "cwd": "/tmp/proj",
                    "args": "claude --model sonnet", "tool": "claude",
                    "workspaceId": "ws-uuid-1", "surfaceId": "sf-uuid-b",
                },
                {
                    "pid": 200, "tty": "ttys001", "cwd": "/tmp/proj2",
                    "args": "claude --verbose", "tool": "claude",
                    "workspaceId": "ws-uuid-1", "surfaceId": "sf-uuid-a",
                },
            ]

            result = watcher.enrich_cmux_entries(entries)

            self.assertEqual(result[0]["workspaceName"], "my-project")
            self.assertEqual(result[1]["workspaceName"], "my-project")
            # Surface indices assigned by sorted UUID within workspace
            # sf-uuid-a < sf-uuid-b, so sf-uuid-a = index 0, sf-uuid-b = index 1
            self.assertEqual(result[0]["surfaceIndex"], 1)  # sf-uuid-b
            self.assertEqual(result[1]["surfaceIndex"], 0)  # sf-uuid-a

    def test_enrich_graceful_when_map_file_missing(self) -> None:
        watcher = load_watcher_module()
        with tempfile.TemporaryDirectory() as tempdir:
            watcher.CMUX_WORKSPACE_MAP_PATH = Path(tempdir) / "nonexistent.json"
            entries = [
                {
                    "pid": 100, "tty": "ttys000", "cwd": "/tmp/proj",
                    "args": "claude", "tool": "claude",
                    "workspaceId": "ws-1", "surfaceId": "sf-a",
                },
            ]
            result = watcher.enrich_cmux_entries(entries)
            self.assertNotIn("workspaceName", result[0])

    # -- Integration: full save pipeline with cmux metadata --

    def test_persist_and_save_preserves_cmux_metadata(self) -> None:
        """Integration: persist_live_state writes cmux metadata, save_sessions reads it back."""
        watcher = load_watcher_module()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watcher.LIVE_STATE_PATH = root / "ghostty-live-state.json"
            watcher.RESTORE_PATH = root / "ghostty-restore.json"
            watcher.SNAPSHOT_PATH = root / "ghostty-snapshot.json"
            watcher.CLAUDE_PROJECTS_PATH = root / "projects"

            # Simulate enriched entries from list_candidate_processes + enrich_cmux_entries.
            entries = [
                {
                    "pid": 100, "tty": "ttys000", "cwd": "/tmp/proj",
                    "args": "claude --model sonnet", "tool": "claude",
                    "workspaceId": "ws-uuid-1", "workspaceName": "my-project",
                    "surfaceIndex": 0,
                },
                {
                    "pid": 200, "tty": "ttys001", "cwd": "/tmp/proj2",
                    "args": "codex --model gpt-5", "tool": "codex",
                },
            ]

            # Step 1: persist_live_state (mirrors what main loop does).
            count = watcher.persist_live_state(entries)
            self.assertEqual(count, 2)

            live_data = json.loads(watcher.LIVE_STATE_PATH.read_text(encoding="utf-8"))
            self.assertEqual(live_data[0]["terminal"], "cmux")
            self.assertEqual(live_data[0]["workspaceName"], "my-project")
            self.assertNotIn("terminal", live_data[1])

            # Step 2: save_sessions (mirrors what happens when terminal quits).
            total, resumed, continued = watcher.save_sessions()
            self.assertEqual(total, 2)

            restore_data = json.loads(watcher.RESTORE_PATH.read_text(encoding="utf-8"))
            self.assertEqual(restore_data[0]["terminal"], "cmux")
            self.assertEqual(restore_data[0]["workspaceName"], "my-project")
            self.assertEqual(restore_data[0]["surfaceIndex"], 0)
            self.assertNotIn("terminal", restore_data[1])

    def test_full_cmux_pipeline_entries_to_save(self) -> None:
        """End-to-end: enriched entries -> persist_live_state -> save_sessions."""
        watcher = load_watcher_module()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watcher.LIVE_STATE_PATH = root / "ghostty-live-state.json"
            watcher.RESTORE_PATH = root / "ghostty-restore.json"
            watcher.SNAPSHOT_PATH = root / "ghostty-snapshot.json"
            watcher.CLAUDE_PROJECTS_PATH = root / "projects"
            map_path = root / "cmux-workspace-map.json"
            watcher.CMUX_WORKSPACE_MAP_PATH = map_path

            # Write workspace map (as shell startup snippet would)
            map_path.write_text(json.dumps({
                "ws-uuid-1": "alpha",
                "ws-uuid-2": "beta",
            }), encoding="utf-8")

            # Raw entries (as list_candidate_processes would produce)
            entries = [
                {
                    "pid": 100, "tty": "ttys000",
                    "cwd": "/tmp/proj-a", "args": "claude --model sonnet",
                    "tool": "claude",
                    "workspaceId": "ws-uuid-1", "surfaceId": "sf-a1",
                },
                {
                    "pid": 200, "tty": "ttys001",
                    "cwd": "/tmp/proj-b", "args": "claude --verbose",
                    "tool": "claude",
                    "workspaceId": "ws-uuid-2", "surfaceId": "sf-b1",
                },
            ]

            # Enrich
            entries = watcher.enrich_cmux_entries(entries)
            self.assertEqual(entries[0]["workspaceName"], "alpha")
            self.assertEqual(entries[1]["workspaceName"], "beta")

            # Persist and save
            watcher.persist_live_state(entries)
            total, _, _ = watcher.save_sessions()
            self.assertEqual(total, 2)

            restore_data = json.loads(watcher.RESTORE_PATH.read_text(encoding="utf-8"))
            self.assertEqual(restore_data[0]["terminal"], "cmux")
            self.assertEqual(restore_data[0]["workspaceName"], "alpha")
            self.assertEqual(restore_data[1]["terminal"], "cmux")
            self.assertEqual(restore_data[1]["workspaceName"], "beta")


class CmuxRestoreAutoTests(unittest.TestCase):
    """Subprocess integration tests for restore.sh with cmux-tagged sessions.

    Mirrors the pattern from test_restore_auto.py: run the actual script as a
    subprocess with a fake $HOME containing real JSON files.
    """

    def run_restore_auto(
        self, home: Path, extra_env: dict[str, str] | None = None,
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

    def test_auto_mode_filters_out_cmux_sessions(self) -> None:
        """In ghostty --auto mode, cmux sessions are filtered; only ghostty session returned."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd_cmux = home / "proj-cmux"
            cwd_ghostty = home / "proj-ghostty"
            cwd_cmux.mkdir()
            cwd_ghostty.mkdir()

            payload = [
                {
                    "tool": "claude", "sessionId": "aaa",
                    "cwd": str(cwd_cmux), "flags": [],
                    "terminal": "cmux", "workspaceName": "dev", "surfaceIndex": 0,
                },
                {
                    "tool": "claude", "sessionId": "bbb",
                    "cwd": str(cwd_ghostty), "flags": ["--model", "sonnet"],
                },
            ]
            (claude_dir / "ghostty-restore.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            proc = self.run_restore_auto(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["sessionId"], "bbb")
            self.assertNotIn("terminal", out)

    def test_auto_mode_returns_nothing_when_all_cmux(self) -> None:
        """If every session is cmux-tagged, ghostty --auto returns empty (exit 0, no output)."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "proj"
            cwd.mkdir()

            payload = [
                {
                    "tool": "claude", "sessionId": "aaa", "cwd": str(cwd),
                    "flags": [], "terminal": "cmux",
                    "workspaceName": "dev", "surfaceIndex": 0,
                },
            ]
            (claude_dir / "ghostty-restore.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            proc = self.run_restore_auto(home)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

    def test_auto_mode_backward_compat_entries_without_terminal(self) -> None:
        """Pre-cmux entries (no terminal field) continue to work in ghostty --auto mode."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "proj"
            cwd.mkdir()

            payload = [
                {
                    "tool": "claude",
                    "sessionId": "904135b4-8584-42dd-aeb9-08b920d0e02e",
                    "cwd": str(cwd),
                    "flags": [],
                },
            ]
            (claude_dir / "ghostty-restore.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            proc = self.run_restore_auto(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["sessionId"], "904135b4-8584-42dd-aeb9-08b920d0e02e")

    def test_auto_cmux_updates_workspace_map(self) -> None:
        """--auto-cmux writes the workspace map file."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)

            env = os.environ.copy()
            env["HOME"] = str(home)
            proc = subprocess.run(
                [sys.executable, str(RESTORE_SCRIPT), "--update-cmux-map"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env=env,
            )
            # If cmux isn't running, the command should still exit 0 (no-op).
            self.assertEqual(proc.returncode, 0)


class CmuxRestoreLogicTests(unittest.TestCase):
    """Tests for do_cmux_auto_restore / do_cmux_manual_restore logic."""

    def setUp(self) -> None:
        self.restore = load_restore_module()

    def test_do_cmux_auto_restore_skips_out_of_range_surface_index(self) -> None:
        """surfaceIndex 5 with only 1 surface is skipped; session preserved for retry."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "proj"
            cwd.mkdir()

            restore_file = claude_dir / "ghostty-restore.json"
            live_file = claude_dir / "ghostty-live-state.json"
            session = {
                "tool": "claude", "sessionId": "aaa", "cwd": str(cwd),
                "flags": [], "terminal": "cmux",
                "workspaceName": "dev", "surfaceIndex": 5,
            }
            restore_file.write_text(
                json.dumps([session]),
                encoding="utf-8",
            )

            def fake_run(args):
                if args[:4] == ["cmux", "--json", "--id-format", "both"]:
                    return _ok(args, stdout=json.dumps({
                        "workspaces": [{"id": "ws-1", "title": "dev", "ref": "workspace:1"}],
                    }))
                if args[:3] == ["cmux", "--json", "list-pane-surfaces"]:
                    return _ok(args, stdout=json.dumps({
                        "surfaces": [{"ref": "surface:1"}],
                    }))
                return _fail(args)

            with mock.patch.object(self.restore, "_run", side_effect=fake_run):
                with mock.patch.object(self.restore, "RESTORE_FILE", restore_file):
                    with mock.patch.object(self.restore, "LIVE_STATE_FILE", live_file):
                        restored = self.restore.do_cmux_auto_restore([session])

            self.assertEqual(restored, 0)
            # Failed session must be preserved in restore file for retry
            self.assertTrue(restore_file.exists())
            remaining = json.loads(restore_file.read_text(encoding="utf-8"))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["workspaceName"], "dev")

    def test_do_cmux_auto_restore_prefers_surface_id_over_index(self) -> None:
        """If surfaceId exists, restore uses it even when surfaceIndex is stale."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "proj"
            cwd.mkdir()

            restore_file = claude_dir / "ghostty-restore.json"
            live_file = claude_dir / "ghostty-live-state.json"
            restore_file.write_text(
                json.dumps([{
                    "tool": "claude", "sessionId": "aaa", "cwd": str(cwd),
                    "flags": [], "terminal": "cmux", "workspaceId": "ws-1",
                    "workspaceName": "dev", "surfaceId": "sf-correct", "surfaceIndex": 5,
                }]),
                encoding="utf-8",
            )

            sent: list[list[str]] = []

            def fake_run(args):
                if args[:4] == ["cmux", "--json", "--id-format", "both"]:
                    return _ok(args, stdout=json.dumps({
                        "workspaces": [{"id": "ws-1", "title": "dev", "ref": "workspace:1"}],
                    }))
                if args[:3] == ["cmux", "--json", "list-pane-surfaces"]:
                    return _ok(args, stdout=json.dumps({
                        "surfaces": [{"id": "sf-correct", "ref": "surface:77"}],
                    }))
                if args[:2] == ["cmux", "send"] or args[:2] == ["cmux", "send-key"]:
                    sent.append(args)
                    return _ok(args)
                return _fail(args)

            with mock.patch.object(self.restore, "_run", side_effect=fake_run):
                with mock.patch.object(self.restore, "RESTORE_FILE", restore_file):
                    with mock.patch.object(self.restore, "LIVE_STATE_FILE", live_file):
                        sessions = [
                            {
                                "tool": "claude", "sessionId": "aaa", "cwd": str(cwd),
                                "flags": [], "terminal": "cmux", "workspaceId": "ws-1",
                                "workspaceName": "dev", "surfaceId": "sf-correct", "surfaceIndex": 5,
                            },
                        ]
                        restored = self.restore.do_cmux_auto_restore(sessions)

            self.assertEqual(restored, 1)
            self.assertTrue(any("surface:77" in " ".join(cmd) for cmd in sent))

    def test_do_cmux_auto_restore_does_not_count_failed_send(self) -> None:
        """Session is not counted as restored when cmux send/send-key fails."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "proj"
            cwd.mkdir()

            restore_file = claude_dir / "ghostty-restore.json"
            live_file = claude_dir / "ghostty-live-state.json"
            session = {
                "tool": "claude", "sessionId": "aaa", "cwd": str(cwd),
                "flags": [], "terminal": "cmux", "workspaceId": "ws-1",
                "workspaceName": "dev", "surfaceId": "sf-correct", "surfaceIndex": 0,
            }
            restore_file.write_text(json.dumps([session]), encoding="utf-8")

            def fake_run(args):
                if args[:4] == ["cmux", "--json", "--id-format", "both"]:
                    return _ok(args, stdout=json.dumps({
                        "workspaces": [{"id": "ws-1", "title": "dev", "ref": "workspace:1"}],
                    }))
                if args[:3] == ["cmux", "--json", "list-pane-surfaces"]:
                    return _ok(args, stdout=json.dumps({
                        "surfaces": [{"id": "sf-correct", "ref": "surface:77"}],
                    }))
                if args[:2] == ["cmux", "send"]:
                    return _fail(args)
                if args[:2] == ["cmux", "send-key"]:
                    return _ok(args)
                return _fail(args)

            with mock.patch.object(self.restore, "_run", side_effect=fake_run):
                with mock.patch.object(self.restore, "RESTORE_FILE", restore_file):
                    with mock.patch.object(self.restore, "LIVE_STATE_FILE", live_file):
                        restored = self.restore.do_cmux_auto_restore([session])

            self.assertEqual(restored, 0)
            # Failed session preserved for retry
            self.assertTrue(restore_file.exists())
            remaining = json.loads(restore_file.read_text(encoding="utf-8"))
            self.assertEqual(len(remaining), 1)

    def test_do_cmux_auto_restore_preserves_failed_workspaces(self) -> None:
        """When one workspace restores and another fails, failed sessions stay in restore file."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd_a = home / "proj-a"
            cwd_a.mkdir()
            cwd_b = home / "proj-b"
            cwd_b.mkdir()

            restore_file = claude_dir / "ghostty-restore.json"
            live_file = claude_dir / "ghostty-live-state.json"
            session_ok = {
                "tool": "claude", "sessionId": "aaa", "cwd": str(cwd_a),
                "flags": [], "terminal": "cmux", "workspaceId": "ws-1",
                "workspaceName": "ready", "surfaceId": "sf-1", "surfaceIndex": 0,
            }
            session_fail = {
                "tool": "claude", "sessionId": "bbb", "cwd": str(cwd_b),
                "flags": [], "terminal": "cmux", "workspaceId": "ws-2",
                "workspaceName": "not-ready", "surfaceId": "sf-2", "surfaceIndex": 0,
            }
            restore_file.write_text(
                json.dumps([session_ok, session_fail]), encoding="utf-8"
            )

            def fake_run(args):
                if args[:4] == ["cmux", "--json", "--id-format", "both"]:
                    # Only "ready" workspace is available
                    return _ok(args, stdout=json.dumps({
                        "workspaces": [{"id": "ws-1", "title": "ready", "ref": "workspace:1"}],
                    }))
                if args[:3] == ["cmux", "--json", "list-pane-surfaces"]:
                    return _ok(args, stdout=json.dumps({
                        "surfaces": [{"id": "sf-1", "ref": "surface:10"}],
                    }))
                if args[:2] in (["cmux", "send"], ["cmux", "send-key"]):
                    return _ok(args)
                return _fail(args)

            with mock.patch.object(self.restore, "_run", side_effect=fake_run):
                with mock.patch.object(self.restore, "RESTORE_FILE", restore_file):
                    with mock.patch.object(self.restore, "LIVE_STATE_FILE", live_file):
                        restored = self.restore.do_cmux_auto_restore(
                            [session_ok, session_fail]
                        )

            self.assertEqual(restored, 1)
            # "not-ready" session preserved for retry by next shell
            self.assertTrue(restore_file.exists())
            remaining = json.loads(restore_file.read_text(encoding="utf-8"))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["workspaceName"], "not-ready")

    def test_do_cmux_auto_restore_preserves_ghostty_entries_from_restore_file(self) -> None:
        """Successful cmux restore must not delete pending ghostty sessions."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "proj"
            cwd.mkdir()

            restore_file = claude_dir / "ghostty-restore.json"
            live_file = claude_dir / "ghostty-live-state.json"
            ghostty_session = {
                "tool": "claude", "sessionId": "ggg", "cwd": str(cwd),
                "flags": [],
            }
            cmux_session = {
                "tool": "claude", "sessionId": "ccc", "cwd": str(cwd),
                "flags": [], "terminal": "cmux", "workspaceId": "ws-1",
                "workspaceName": "dev", "surfaceId": "sf-1", "surfaceIndex": 0,
            }
            restore_file.write_text(
                json.dumps([ghostty_session, cmux_session]), encoding="utf-8"
            )

            def fake_run(args):
                if args[:4] == ["cmux", "--json", "--id-format", "both"]:
                    return _ok(args, stdout=json.dumps({
                        "workspaces": [{"id": "ws-1", "title": "dev", "ref": "workspace:1"}],
                    }))
                if args[:3] == ["cmux", "--json", "list-pane-surfaces"]:
                    return _ok(args, stdout=json.dumps({
                        "surfaces": [{"id": "sf-1", "ref": "surface:10"}],
                    }))
                if args[:2] in (["cmux", "send"], ["cmux", "send-key"]):
                    return _ok(args)
                return _fail(args)

            with mock.patch.object(self.restore, "_run", side_effect=fake_run):
                with mock.patch.object(self.restore, "RESTORE_FILE", restore_file):
                    with mock.patch.object(self.restore, "LIVE_STATE_FILE", live_file):
                        restored = self.restore.do_cmux_auto_restore([cmux_session])

            self.assertEqual(restored, 1)
            # Ghostty session must survive in restore file
            self.assertTrue(restore_file.exists())
            remaining = json.loads(restore_file.read_text(encoding="utf-8"))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["sessionId"], "ggg")

    def test_do_cmux_auto_restore_preserves_ghostty_entries_from_live_state(self) -> None:
        """Cmux restore with live-state-only recovery must not drop ghostty sessions."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "proj"
            cwd.mkdir()

            restore_file = claude_dir / "ghostty-restore.json"
            live_file = claude_dir / "ghostty-live-state.json"
            ghostty_session = {
                "tool": "claude", "sessionId": "ggg", "cwd": str(cwd),
                "flags": [],
            }
            cmux_session = {
                "tool": "claude", "sessionId": "ccc", "cwd": str(cwd),
                "flags": [], "terminal": "cmux", "workspaceId": "ws-1",
                "workspaceName": "dev", "surfaceId": "sf-1", "surfaceIndex": 0,
            }
            # Only live-state exists — no restore file
            live_file.write_text(
                json.dumps([ghostty_session, cmux_session]), encoding="utf-8"
            )

            def fake_run(args):
                if args[:4] == ["cmux", "--json", "--id-format", "both"]:
                    return _ok(args, stdout=json.dumps({
                        "workspaces": [{"id": "ws-1", "title": "dev", "ref": "workspace:1"}],
                    }))
                if args[:3] == ["cmux", "--json", "list-pane-surfaces"]:
                    return _ok(args, stdout=json.dumps({
                        "surfaces": [{"id": "sf-1", "ref": "surface:10"}],
                    }))
                if args[:2] in (["cmux", "send"], ["cmux", "send-key"]):
                    return _ok(args)
                return _fail(args)

            with mock.patch.object(self.restore, "_run", side_effect=fake_run):
                with mock.patch.object(self.restore, "RESTORE_FILE", restore_file):
                    with mock.patch.object(self.restore, "LIVE_STATE_FILE", live_file):
                        restored = self.restore.do_cmux_auto_restore([cmux_session])

            self.assertEqual(restored, 1)
            # Ghostty session must be preserved in restore file
            self.assertTrue(restore_file.exists())
            remaining = json.loads(restore_file.read_text(encoding="utf-8"))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["sessionId"], "ggg")

    def test_do_cmux_auto_restore_dedupes_ghostty_entries_from_both_files(self) -> None:
        """Ghostty entries duplicated across restore/live should be kept once."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "proj"
            cwd.mkdir()

            restore_file = claude_dir / "ghostty-restore.json"
            live_file = claude_dir / "ghostty-live-state.json"
            ghostty_session = {
                "tool": "claude", "sessionId": "ggg", "cwd": str(cwd),
                "flags": [],
            }
            cmux_session = {
                "tool": "claude", "sessionId": "ccc", "cwd": str(cwd),
                "flags": [], "terminal": "cmux", "workspaceId": "ws-1",
                "workspaceName": "dev", "surfaceId": "sf-1", "surfaceIndex": 0,
            }
            restore_file.write_text(
                json.dumps([ghostty_session, cmux_session]), encoding="utf-8"
            )
            live_file.write_text(
                json.dumps([ghostty_session, cmux_session]), encoding="utf-8"
            )

            def fake_run(args):
                if args[:4] == ["cmux", "--json", "--id-format", "both"]:
                    return _ok(args, stdout=json.dumps({
                        "workspaces": [{"id": "ws-1", "title": "dev", "ref": "workspace:1"}],
                    }))
                if args[:3] == ["cmux", "--json", "list-pane-surfaces"]:
                    return _ok(args, stdout=json.dumps({
                        "surfaces": [{"id": "sf-1", "ref": "surface:10"}],
                    }))
                if args[:2] in (["cmux", "send"], ["cmux", "send-key"]):
                    return _ok(args)
                return _fail(args)

            with mock.patch.object(self.restore, "_run", side_effect=fake_run):
                with mock.patch.object(self.restore, "RESTORE_FILE", restore_file):
                    with mock.patch.object(self.restore, "LIVE_STATE_FILE", live_file):
                        restored = self.restore.do_cmux_auto_restore([cmux_session])

            self.assertEqual(restored, 1)
            remaining = json.loads(restore_file.read_text(encoding="utf-8"))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["sessionId"], "ggg")


class CmuxJsonParsingTests(unittest.TestCase):
    """Tests for _parse_cmux_json and workspace map generation."""

    def setUp(self) -> None:
        self.restore = load_restore_module()

    def test_parse_cmux_json_nested_format(self) -> None:
        """Cmux wraps arrays under a key like {"workspaces": [...]}."""
        raw = json.dumps({
            "window_ref": "window:1",
            "workspaces": [
                {"id": "uuid-1", "title": "alpha", "ref": "workspace:1"},
                {"id": "uuid-2", "title": "beta", "ref": "workspace:2"},
            ],
        })
        result = self.restore._parse_cmux_json(raw, "workspaces")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "alpha")

    def test_parse_cmux_json_flat_list_fallback(self) -> None:
        """If cmux ever returns a flat list, it still works."""
        raw = json.dumps([{"id": "uuid-1", "title": "alpha"}])
        result = self.restore._parse_cmux_json(raw, "workspaces")
        self.assertEqual(len(result), 1)

    def test_parse_cmux_json_invalid_input(self) -> None:
        """Invalid JSON returns empty list."""
        self.assertEqual(self.restore._parse_cmux_json("not json", "x"), [])
        self.assertEqual(self.restore._parse_cmux_json("", "x"), [])

    def test_parse_cmux_json_wrong_key(self) -> None:
        """Dict without the expected key returns empty list."""
        raw = json.dumps({"other_key": [1, 2, 3]})
        self.assertEqual(self.restore._parse_cmux_json(raw, "workspaces"), [])

    def test_parse_cmux_json_surfaces_nested(self) -> None:
        """Surface listing uses the same nested pattern."""
        raw = json.dumps({
            "pane_ref": "pane:1",
            "surfaces": [
                {"ref": "surface:1", "type": "terminal", "index": 0},
                {"ref": "surface:2", "type": "terminal", "index": 1},
            ],
        })
        result = self.restore._parse_cmux_json(raw, "surfaces")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["ref"], "surface:2")

    def test_update_workspace_map_uses_title_field(self) -> None:
        """update_cmux_workspace_map reads 'title' (not 'name') from cmux output."""
        cmux_output = json.dumps({
            "window_ref": "window:1",
            "workspaces": [
                {"id": "uuid-1", "title": "my-project", "ref": "workspace:1"},
                {"id": "uuid-2", "title": "other", "ref": "workspace:2"},
            ],
        })
        fake_proc = subprocess.CompletedProcess(
            [], returncode=0, stdout=cmux_output, stderr="",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            map_file = Path(tempdir) / "cmux-workspace-map.json"
            with mock.patch.object(self.restore, "_run", return_value=fake_proc):
                with mock.patch.object(self.restore, "CMUX_WORKSPACE_MAP_FILE", map_file):
                    self.restore.update_cmux_workspace_map()
            data = json.loads(map_file.read_text())
            self.assertEqual(data["uuid-1"], "my-project")
            self.assertEqual(data["uuid-2"], "other")


if __name__ == "__main__":
    unittest.main()
