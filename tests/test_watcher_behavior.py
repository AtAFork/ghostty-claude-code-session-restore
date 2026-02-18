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

        with mock.patch.object(watcher, "is_ghostty_running", return_value=False):
            with mock.patch.object(watcher, "load_snapshot", return_value=[]):
                self.assertFalse(watcher.should_save_on_shutdown())

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


if __name__ == "__main__":
    unittest.main()
