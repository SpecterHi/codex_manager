import json
import tempfile
import threading
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import codex_sessions_web as web  # noqa: E402
import codex_sessions  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class SessionEventsTest(unittest.TestCase):
    def test_set_session_title_refreshes_thread_state_and_session_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / ".codex"
            session_path = codex_home / "sessions" / "2026" / "04" / "22" / "rollout-2026-04-22T10-00-00-demo.jsonl"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            write_jsonl(
                session_path,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-title-refresh",
                            "timestamp": "2026-04-22T10:00:00Z",
                            "cwd": "/tmp/demo",
                            "originator": "vscode",
                            "source": "vscode",
                            "title": "old title",
                        },
                    }
                ],
            )

            db_path = codex_home / "state_5.sqlite"
            conn = codex_sessions.sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, title TEXT, cwd TEXT, updated_at INTEGER)"
                )
                conn.execute(
                    "INSERT INTO threads (id, source, title, cwd, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("sess-title-refresh", "vscode", "old title", "/tmp/demo", 123),
                )
                conn.commit()
            finally:
                conn.close()

            index_path = codex_home / "session_index.jsonl"
            index_path.write_text(
                json.dumps(
                    {
                        "id": "sess-title-refresh",
                        "thread_name": "old title",
                        "updated_at": "2026-04-22T10:00:00Z",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            records = codex_sessions.load_records(
                codex_home=codex_home,
                include_archived=True,
                slack_db=None,
                aliases_db=codex_home / "session_aliases.json",
            )
            record = next(item for item in records if item.session_id == "sess-title-refresh")

            with mock.patch.object(
                codex_sessions,
                "sync_official_title_to_targets",
                return_value={"official_title_sync": "skipped", "official_title_sync_attempted": False},
            ):
                result = codex_sessions.set_session_title(
                    record,
                    codex_home / "session_aliases.json",
                    "new visible title",
                    codex_home=codex_home,
                )

            self.assertTrue(result["session_index_appended"])

            conn = codex_sessions.sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT title, updated_at FROM threads WHERE id = ?",
                    ("sess-title-refresh",),
                ).fetchone()
            finally:
                conn.close()
            assert row is not None
            self.assertEqual(row[0], "new visible title")
            self.assertGreater(int(row[1]), 123)

            latest_entry = None
            for raw in index_path.read_text(encoding="utf-8").splitlines():
                payload = json.loads(raw)
                if payload.get("id") == "sess-title-refresh":
                    latest_entry = payload
            assert latest_entry is not None
            self.assertEqual(latest_entry["thread_name"], "new visible title")

    def test_load_records_distinguishes_active_archived_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / ".codex"
            active_path = codex_home / "sessions" / "2026" / "04" / "21" / "rollout-2026-04-21T10-00-00-active.jsonl"
            archived_path = codex_home / "archived_sessions" / "rollout-2026-04-20T10-00-00-archived.jsonl"
            deleted_path = codex_home / "deleted_sessions" / "rollout-2026-04-19T10-00-00-deleted.jsonl"
            for path, session_id in [
                (active_path, "sess-active"),
                (archived_path, "sess-archived"),
                (deleted_path, "sess-deleted"),
            ]:
                path.parent.mkdir(parents=True, exist_ok=True)
                write_jsonl(
                    path,
                    [
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": session_id,
                                "timestamp": "2026-04-21T10:00:00Z",
                                "cwd": "/tmp/demo",
                                "originator": "vscode",
                                "source": "vscode",
                            },
                        }
                    ],
                )

            records = codex_sessions.load_records(
                codex_home=codex_home,
                include_archived=True,
                slack_db=None,
                aliases_db=codex_home / "session_aliases.json",
            )
            states = {record.session_id: codex_sessions.session_record_state(record) for record in records}
            self.assertEqual(states["sess-active"], "active")
            self.assertEqual(states["sess-archived"], "archived")
            self.assertEqual(states["sess-deleted"], "deleted")

    def test_build_stats_counts_deleted_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / ".codex"
            active_path = codex_home / "sessions" / "2026" / "04" / "21" / "rollout-2026-04-21T10-00-00-active.jsonl"
            archived_path = codex_home / "archived_sessions" / "rollout-2026-04-20T10-00-00-archived.jsonl"
            deleted_path = codex_home / "deleted_sessions" / "rollout-2026-04-19T10-00-00-deleted.jsonl"
            for path, session_id in [
                (active_path, "sess-active"),
                (archived_path, "sess-archived"),
                (deleted_path, "sess-deleted"),
            ]:
                path.parent.mkdir(parents=True, exist_ok=True)
                write_jsonl(
                    path,
                    [
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": session_id,
                                "timestamp": "2026-04-21T10:00:00Z",
                                "cwd": "/tmp/demo",
                                "originator": "vscode",
                                "source": "vscode",
                            },
                        }
                    ],
                )

            app = web.AppContext(
                codex_home=codex_home,
                slack_db=None,
                aliases_db=codex_home / "session_aliases.json",
                codex_bin="codex",
                auth=None,
                targets_path=codex_home / "targets.json",
                remote_marks_path=codex_home / "marks.json",
                remote_watchlist_path=codex_home / "watch.json",
                supervisor_lock_path=codex_home / "lock",
                lock=threading.Lock(),
                targets={},
                resume_jobs={},
                auth_failures={},
                remote_marks={},
                remote_watchlist={},
                shutdown_event=threading.Event(),
            )
            stats = web.build_stats(app, include_archived=True, query="", source_label="")
            self.assertEqual(stats["total"], 3)
            self.assertEqual(stats["active"], 1)
            self.assertEqual(stats["archived"], 1)
            self.assertEqual(stats["deleted"], 1)

    def test_parse_target_base_url_accepts_loopback_default(self) -> None:
        host, port = web.parse_target_base_url("http://127.0.0.1:8765")
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 8765)

    def test_build_target_from_payload_keeps_password_auth(self) -> None:
        target = web.build_target_from_payload(
            {
                "label": "remote-a",
                "ssh_host": "192.168.1.9",
                "ssh_user": "ubuntu",
                "ssh_port": 2222,
                "base_url": "http://127.0.0.1:9000",
                "auth_mode": "password",
                "ssh_password": "secret",
            }
        )
        self.assertEqual(target.target_id, "ubuntu@192.168.1.9:2222")
        self.assertEqual(target.base_url, "http://127.0.0.1:9000")
        self.assertEqual(target.auth_mode, "password")
        self.assertEqual(target.ssh_password, "secret")

    def test_save_and_load_machine_targets_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "targets.json"
            targets = {
                web.LOCAL_TARGET_ID: web.build_local_target(),
                "ubuntu@example.com:22": web.MachineTarget(
                    target_id="ubuntu@example.com:22",
                    label="Example",
                    kind="ssh",
                    ssh_host="example.com",
                    ssh_user="ubuntu",
                    ssh_port=22,
                    base_url="http://127.0.0.1:8765",
                    auth_mode="password",
                ),
            }
            web.save_machine_targets(path, targets)
            loaded = web.load_machine_targets(path)
            self.assertIn(web.LOCAL_TARGET_ID, loaded)
            self.assertIn("ubuntu@example.com:22", loaded)
            self.assertEqual(loaded["ubuntu@example.com:22"].label, "Example")
            self.assertEqual(loaded["ubuntu@example.com:22"].auth_mode, "password")

    def test_enrich_target_check_result_marks_version_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "codex_sessions_web.py").write_text("print('ok')\n", encoding="utf-8")
            local_release = web.build_release_metadata(repo_root)
            result = web.enrich_target_check_result(
                {
                    "ok": True,
                    "api_ready": True,
                    "compat_sessions": True,
                    "compat_remote_sessions": True,
                    "compat_events": True,
                    "compat_session_id": "sess-1",
                    "codex_ok": True,
                    "release_metadata": dict(local_release),
                },
                repo_root,
            )
            self.assertTrue(result["version_match"])
            self.assertEqual(result["recommendation"], "ready")
            self.assertEqual(result["remote_version_label"], result["local_version_label"])

    def test_enrich_target_check_result_marks_update_when_remote_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "codex_sessions_web.py").write_text("print('ok')\n", encoding="utf-8")
            result = web.enrich_target_check_result(
                {
                    "ok": True,
                    "api_ready": True,
                    "compat_sessions": True,
                    "compat_remote_sessions": True,
                    "compat_events": True,
                    "compat_session_id": "sess-1",
                    "codex_ok": True,
                    "release_metadata": {"content_digest": "deadbeef" * 8, "git_commit_short": "old1234"},
                },
                repo_root,
            )
            self.assertFalse(result["version_match"])
            self.assertEqual(result["recommendation"], "update_recommended")

    def test_enrich_target_check_result_marks_resume_unavailable_without_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "codex_sessions_web.py").write_text("print('ok')\n", encoding="utf-8")
            result = web.enrich_target_check_result(
                {
                    "ok": True,
                    "api_ready": True,
                    "compat_sessions": True,
                    "compat_remote_sessions": True,
                    "compat_events": True,
                    "compat_session_id": "sess-1",
                    "codex_ok": False,
                    "release_metadata": dict(web.build_release_metadata(repo_root)),
                },
                repo_root,
            )
            self.assertEqual(result["recommendation"], "resume_unavailable")

    def test_enrich_target_check_result_requires_upgrade_when_capability_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "codex_sessions_web.py").write_text("print('ok')\n", encoding="utf-8")
            result = web.enrich_target_check_result(
                {
                    "ok": True,
                    "api_ready": True,
                    "compat_sessions": True,
                    "compat_remote_sessions": False,
                    "compat_events": False,
                    "compat_session_id": "",
                    "release_metadata": web.build_release_metadata(repo_root),
                },
                repo_root,
            )
            self.assertFalse(result["compat_ready"])
            self.assertEqual(result["recommendation"], "upgrade_required_for_ui")

    def test_enrich_target_check_result_keeps_legacy_process_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "codex_sessions_web.py").write_text("print('ok')\n", encoding="utf-8")
            result = web.enrich_target_check_result(
                {
                    "ok": True,
                    "api_ready": False,
                    "compat_sessions": False,
                    "compat_remote_sessions": False,
                    "compat_events": False,
                    "compat_session_id": "",
                    "recommendation": "legacy_process_conflict",
                    "listener_command": "python3 /home/dell/codex_manager/codex_sessions_web.py --port 8765",
                },
                repo_root,
            )
            self.assertEqual(result["recommendation"], "legacy_process_conflict")

    def test_parse_tool_output_event_extracts_command_and_exit_code(self) -> None:
        obj = {
            "type": "response_item",
            "timestamp": "2026-04-09T10:00:00Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": (
                    "Command: /bin/bash -lc pwd\n"
                    "Chunk ID: abc123\n"
                    "Wall time: 0.0000 seconds\n"
                    "Process exited with code 0\n"
                    "Output:\n"
                    "/tmp/demo\n"
                ),
            },
        }
        event = web.parse_session_event(obj)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["kind"], "tool_output")
        self.assertEqual(event["command"], "/bin/bash -lc pwd")
        self.assertEqual(event["exit_code"], 0)
        self.assertIn("/tmp/demo", event["text"])

    def test_recent_and_delta_event_reads_work_with_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "session.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-04-09T10:00:00Z",
                        "payload": {"type": "task_started"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-04-09T10:00:01Z",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Inspecting the repo now.",
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-04-09T10:00:02Z",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": "{\"cmd\":\"pwd\"}",
                            "call_id": "call_1",
                        },
                    },
                ],
            )

            events, cursor = web.read_recent_session_events(path, limit=20)
            self.assertEqual([event["kind"] for event in events], ["task_started", "commentary", "tool_call"])
            self.assertGreater(cursor, 0)

            with path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "timestamp": "2026-04-09T10:00:03Z",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": "call_1",
                                "output": "Command: /bin/bash -lc pwd\nProcess exited with code 0\nOutput:\n/tmp/demo\n",
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            delta, next_cursor, reset = web.read_session_events_since(path, cursor=cursor, limit=20)
            self.assertFalse(reset)
            self.assertEqual(len(delta), 1)
            self.assertEqual(delta[0]["kind"], "tool_output")
            self.assertGreaterEqual(next_cursor, cursor)

    def test_resolve_codex_bin_uses_local_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            fake_bin = home / ".local" / "bin" / "codex"
            fake_bin.parent.mkdir(parents=True, exist_ok=True)
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_bin.chmod(0o755)
            with mock.patch.object(web.Path, "home", return_value=home), mock.patch.object(
                web, "discover_vscode_codex_bins", return_value=[]
            ), mock.patch.object(web.shutil, "which", return_value=None):
                resolved = web.resolve_codex_bin("codex")
            self.assertEqual(resolved, str(fake_bin))

    def test_commentary_and_assistant_duplicates_are_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "session.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-04-09T10:00:01.100Z",
                        "turn_id": "turn_1",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Inspecting the repo now.",
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-04-09T10:00:01.200Z",
                        "turn_id": "turn_1",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Inspecting the repo now."}],
                        },
                    },
                ],
            )

            events, _ = web.read_recent_session_events(path, limit=20)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["kind"], "commentary")

    def test_default_continue_prompt_user_duplicates_are_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "session.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "response_item",
                        "timestamp": "2026-04-09T10:00:01.100Z",
                        "turn_id": "turn_2",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": web.DEFAULT_CONTINUE_PROMPT}],
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-04-09T10:00:01.200Z",
                        "turn_id": "turn_2",
                        "payload": {
                            "type": "user_message",
                            "message": web.DEFAULT_CONTINUE_PROMPT,
                        },
                    },
                ],
            )

            events, _ = web.read_recent_session_events(path, limit=20)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["kind"], "user_message")
            self.assertEqual(events[0]["preview"], web.DEFAULT_CONTINUE_PROMPT_LABEL)

    def test_progress_summary_detects_waiting_after_task_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "session.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-04-09T10:00:00Z",
                        "payload": {"type": "task_started"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-04-09T10:00:01Z",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Running tests.",
                        },
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-04-09T10:00:02Z",
                        "payload": {"type": "task_complete"},
                    },
                ],
            )

            progress = web.build_progress_summary(path, remote_running=False, byte_limit=64 * 1024, max_lines=100)
            self.assertEqual(progress["state"], "waiting")
            self.assertEqual(progress["attention_state"], "completed")

    def test_progress_summary_preview_is_compact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "session.jsonl"
            long_text = "这是一段很长的 commentary。 " * 40
            write_jsonl(
                path,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-04-09T10:00:00Z",
                        "payload": {"type": "task_started"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-04-09T10:00:01Z",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": long_text,
                        },
                    },
                ],
            )

            progress = web.build_progress_summary(path, remote_running=True, byte_limit=64 * 1024, max_lines=100)
            self.assertLessEqual(len(progress["preview"]), 220)
            self.assertTrue(progress["preview"].endswith("…"))

    def test_inspect_recent_turn_lifecycle_detects_completed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "session.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn_a"},
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": "turn_a"},
                    },
                ],
            )

            lifecycle = web.inspect_recent_turn_lifecycle(path)
            self.assertFalse(lifecycle["turn_open"])
            self.assertEqual(lifecycle["latest_completed"]["signature"], "turn_a")
            self.assertEqual(lifecycle["latest_settled"]["kind"], "task_complete")

    def test_load_remote_watchlist_keeps_auto_continue_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "watch.json"
            path.write_text(
                json.dumps(
                    {
                        "session-1": {
                            "added_at": "2026-04-12T00:00:00Z",
                            "auto_continue": True,
                            "continue_prompt": "请继续持续推进",
                            "last_resumed_turn_id": "turn_x",
                            "last_resumed_at": "2026-04-12T00:03:00Z",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            items = web.load_remote_watchlist(path)
            self.assertIn("session-1", items)
            item = items["session-1"]
            self.assertTrue(item.auto_continue)
            self.assertEqual(item.continue_prompt, "请继续持续推进")
            self.assertEqual(item.last_resumed_turn_id, "turn_x")
            self.assertEqual(item.last_resumed_at, "2026-04-12T00:03:00Z")

    def test_auto_continue_tick_resumes_only_once_per_completed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex"
            codex_home.mkdir()
            session_path = codex_home / "session.jsonl"
            write_jsonl(
                session_path,
                [
                    {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn_1"}},
                    {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn_1"}},
                ],
            )
            app = web.AppContext(
                codex_home=codex_home,
                slack_db=None,
                aliases_db=codex_home / "aliases.json",
                codex_bin="codex",
                auth=None,
                targets_path=codex_home / "targets.json",
                remote_marks_path=codex_home / "marks.json",
                remote_watchlist_path=codex_home / "watch.json",
                supervisor_lock_path=codex_home / "auto_continue.lock",
                lock=threading.Lock(),
                targets={},
                resume_jobs={},
                auth_failures={},
                remote_marks={},
                remote_watchlist={
                    "session-1": web.RemoteWatch(
                        session_id="session-1",
                        added_at="2026-04-12T00:00:00Z",
                        auto_continue=True,
                        continue_prompt=web.AUTO_CONTINUE_PROMPT,
                    )
                },
                shutdown_event=mock.MagicMock(),
                supervisor_lock_active=True,
            )
            record = SimpleNamespace(session_id="session-1", path=session_path, cwd="")
            calls: list[str] = []

            def fake_launch(app_ctx, record_obj, *, prompt, origin_label):
                calls.append(f"{record_obj.session_id}:{prompt}:{origin_label}")
                process = mock.MagicMock()
                process.poll.return_value = None
                launch = web.ResumeLaunch(
                    session_id=record_obj.session_id,
                    prompt=prompt,
                    started_at="2026-04-12T00:03:00Z",
                    log_path=codex_home / "resume.log",
                    process=process,
                    log_handle=mock.MagicMock(),
                )
                app_ctx.resume_jobs[record_obj.session_id] = launch
                return launch, codex_home, codex_home / "resume.log", None, None

            with mock.patch.object(web, "load_records", return_value=[record]), mock.patch.object(
                web, "launch_resume_for_record", side_effect=fake_launch
            ), mock.patch.object(web, "save_remote_watchlist", return_value=None):
                web.auto_continue_tick(app)
                self.assertEqual(len(calls), 1)
                self.assertEqual(app.remote_watchlist["session-1"].last_resumed_turn_id, "turn_1")
                app.resume_jobs.clear()
                web.auto_continue_tick(app)
                self.assertEqual(len(calls), 1)

                with session_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn_2"}}, ensure_ascii=False) + "\n")
                    fh.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn_2"}}, ensure_ascii=False) + "\n")

                web.auto_continue_tick(app)
                self.assertEqual(len(calls), 2)
                self.assertEqual(app.remote_watchlist["session-1"].last_resumed_turn_id, "turn_2")


if __name__ == "__main__":
    unittest.main()
