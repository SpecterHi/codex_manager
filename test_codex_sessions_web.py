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


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class SessionEventsTest(unittest.TestCase):
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
