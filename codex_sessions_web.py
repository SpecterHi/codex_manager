#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import fcntl
import getpass
import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import signal
import string
import subprocess
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse

from codex_sessions import (
    choose_display_title,
    clear_session_overrides,
    describe_source,
    display_path,
    find_record,
    filter_records,
    load_records,
    set_session_cwd,
    set_session_source,
    set_session_title,
    source_sort_key,
    title_looks_noisy,
)
from codex_manager_release import (
    RELEASE_METADATA_FILENAME,
    build_release_metadata,
    compare_release_metadata,
    version_label,
)


@dataclass
class AppContext:
    codex_home: Path
    slack_db: Optional[Path]
    aliases_db: Path
    codex_bin: str
    auth: Optional["AuthConfig"]
    targets_path: Path
    remote_marks_path: Path
    remote_watchlist_path: Path
    supervisor_lock_path: Path
    lock: threading.Lock
    targets: Dict[str, "MachineTarget"]
    resume_jobs: Dict[str, "ResumeLaunch"]
    auth_failures: Dict[str, "AuthFailureState"]
    remote_marks: Dict[str, "RemoteMark"]
    remote_watchlist: Dict[str, "RemoteWatch"]
    shutdown_event: threading.Event
    supervisor_lock_handle: Optional[Any] = None
    supervisor_lock_active: bool = False


@dataclass
class ResumeLaunch:
    session_id: str
    prompt: str
    started_at: str
    log_path: Path
    process: subprocess.Popen[Any]
    log_handle: Any


@dataclass
class AuthConfig:
    file_path: Path
    password_hash: str
    session_secret: bytes
    cookie_name: str
    session_ttl_seconds: int


@dataclass
class AuthFailureState:
    count: int
    last_attempt: float
    blocked_until: float


@dataclass
class RemoteMark:
    session_id: str
    started_at: str
    prompt: str
    log_path: str


@dataclass
class RemoteWatch:
    session_id: str
    added_at: str
    auto_continue: bool = False
    continue_prompt: str = ""
    last_resumed_turn_id: str = ""
    last_resumed_at: str = ""


@dataclass
class MachineTarget:
    target_id: str
    label: str
    kind: str
    ssh_host: str
    ssh_user: str
    ssh_port: int
    base_url: str
    auth_mode: str = "key"
    ssh_password: str = ""


DEFAULT_CONTINUE_PROMPT_LABEL = "继续自动推进"
DEFAULT_CONTINUE_PROMPT = (
    "继续推进当前任务，直到拿到可验证结果。必要时主动读取代码、修改文件、运行命令或测试，"
    "并在完成后直接汇报结果；不要停在分析、计划或只汇报下一步。"
)
AUTO_CONTINUE_LABEL = "自动续跑"
AUTO_CONTINUE_PROMPT = "请继续持续推进"
AUTO_CONTINUE_INTERVAL_SECONDS = 180
LOCAL_TARGET_ID = "local"
DEFAULT_TARGET_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_REMOTE_INSTALL_BASE = "~/.local/share/codex_manager"
DEFAULT_REMOTE_SERVICE_NAME = "codex-sessions-web.service"
PROXYABLE_GET_PATHS = {
    "/api/sessions",
    "/api/sources",
    "/api/stats",
    "/api/history",
    "/api/events",
    "/api/remote_sessions",
    "/api/remote_guard",
    "/api/progress",
}
PROXYABLE_POST_PATHS = {
    "/api/set_title",
    "/api/rename",
    "/api/clear_title",
    "/api/unname",
    "/api/set_workdir",
    "/api/set_cwd",
    "/api/set_source",
    "/api/archive",
    "/api/delete",
    "/api/batch_archive",
    "/api/batch_delete",
    "/api/resume_cmd",
    "/api/continue",
    "/api/remote_watchlist",
    "/api/stop",
}
REMOTE_API_PROXY_SCRIPT = r"""
import json
import urllib.error
import urllib.request

request_args = REQUEST
base_url = str(request_args.get("base_url") or "")
method = str(request_args.get("method") or "GET")
path = str(request_args.get("path") or "")
body_text = str(request_args.get("body_text") or "")
url = base_url.rstrip("/") + path
payload = body_text.encode("utf-8") if body_text else None
headers = {"Accept": "application/json"}
if body_text:
    headers["Content-Type"] = "application/json"
request = urllib.request.Request(url, data=payload, headers=headers, method=method)

try:
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")
        print(json.dumps({"status": int(response.status), "body": body}, ensure_ascii=False))
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")
    print(json.dumps({"status": int(exc.code), "body": body}, ensure_ascii=False))
except Exception as exc:
    error = {"ok": False, "error": f"Remote API proxy failed: {exc}"}
    print(json.dumps({"status": 599, "body": json.dumps(error, ensure_ascii=False)}, ensure_ascii=False))
"""

REMOTE_TARGET_CHECK_SCRIPT = r"""
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

request_args = REQUEST
service_name = str(request_args.get("service_name") or "codex-sessions-web.service")
bind_host = str(request_args.get("bind_host") or "127.0.0.1")
bind_port = int(request_args.get("bind_port") or 8765)
remote_base = str(request_args.get("remote_base") or "~/.local/share/codex_manager")
release_metadata_filename = ".codex_manager_release.json"


def ok(command):
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return completed.returncode == 0


def capture(command):
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


result = {
    "ok": True,
    "service_name": service_name,
    "bind_host": bind_host,
    "bind_port": bind_port,
    "remote_base": remote_base,
    "python_ok": shutil.which("python3") is not None,
    "curl_ok": shutil.which("curl") is not None,
    "tar_ok": shutil.which("tar") is not None,
}
result["sudo_ok"] = ok(["sudo", "-n", "true"])
result["service_installed"] = False
result["service_enabled"] = False
result["service_active"] = False
result["working_directory"] = ""
result["detected_remote_base"] = remote_base
result["release_metadata"] = None
result["port_in_use"] = False
result["listener_pid"] = ""
result["listener_command"] = ""
result["listener_matches_expected_release"] = False
if result["sudo_ok"]:
    result["service_installed"] = ok(["sudo", "test", "-f", f"/etc/systemd/system/{service_name}"]) or ok(["sudo", "systemctl", "cat", service_name])
    result["service_enabled"] = ok(["sudo", "systemctl", "is-enabled", service_name])
    result["service_active"] = ok(["sudo", "systemctl", "is-active", service_name])
    working_directory = capture(["sudo", "systemctl", "show", service_name, "-p", "WorkingDirectory", "--value"])
    if working_directory.startswith("/"):
        result["working_directory"] = working_directory
        if working_directory.endswith("/current"):
            result["detected_remote_base"] = str(Path(working_directory).parent)
        else:
            result["detected_remote_base"] = working_directory

listener_line = capture(["sudo", "ss", "-ltnp"])
for line in listener_line.splitlines():
    if f":{bind_port} " not in line and not line.rstrip().endswith(f":{bind_port}"):
        continue
    result["port_in_use"] = True
    marker = 'pid='
    if marker in line:
        tail = line.split(marker, 1)[1]
        pid = tail.split(",", 1)[0].split(")", 1)[0].strip()
        if pid:
            result["listener_pid"] = pid
            cmdline = capture(["ps", "-p", pid, "-o", "args="])
            if cmdline:
                result["listener_command"] = cmdline
    break

expected_release_marker = ""
if result["working_directory"]:
    expected_release_marker = str(Path(result["working_directory"]))
elif str(result["detected_remote_base"]).strip():
    expected_release_marker = str(Path(os.path.expanduser(result["detected_remote_base"])) / "current")
if expected_release_marker and result["listener_command"]:
    result["listener_matches_expected_release"] = expected_release_marker in result["listener_command"]

metadata_candidates = []
if result["working_directory"]:
    metadata_candidates.append(Path(result["working_directory"]) / release_metadata_filename)
metadata_candidates.append(Path(os.path.expanduser(result["detected_remote_base"])) / "current" / release_metadata_filename)
seen_paths = set()
for metadata_path in metadata_candidates:
    normalized = str(metadata_path)
    if normalized in seen_paths:
        continue
    seen_paths.add(normalized)
    try:
        result["release_metadata"] = json.loads(metadata_path.read_text(encoding="utf-8"))
        break
    except Exception:
        continue

result["api_ready"] = False
result["auth_enabled"] = False
result["local_bypass"] = False
result["api_error"] = ""
result["compat_sessions"] = False
result["compat_remote_sessions"] = False
result["compat_events"] = False
result["compat_session_id"] = ""
try:
    with urllib.request.urlopen(f"http://{bind_host}:{bind_port}/api/auth/session", timeout=4) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    result["api_ready"] = True
    result["auth_enabled"] = bool(payload.get("auth_enabled"))
    result["local_bypass"] = bool(payload.get("local_bypass"))
except urllib.error.HTTPError as exc:
    result["api_error"] = f"HTTP {int(exc.code)}"
except Exception as exc:
    result["api_error"] = str(exc)

if result["api_ready"]:
    try:
        with urllib.request.urlopen(f"http://{bind_host}:{bind_port}/api/sessions?limit=1", timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if isinstance(payload, dict) and payload.get("ok") is True:
            result["compat_sessions"] = True
            sessions = payload.get("sessions") or []
            if isinstance(sessions, list) and sessions:
                first = sessions[0]
                if isinstance(first, dict):
                    result["compat_session_id"] = str(first.get("id") or "").strip()
    except Exception:
        pass
    try:
        with urllib.request.urlopen(f"http://{bind_host}:{bind_port}/api/remote_sessions?limit=1", timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if isinstance(payload, dict) and payload.get("ok") is True:
            result["compat_remote_sessions"] = True
    except Exception:
        pass
    if result["compat_session_id"]:
        try:
            session_id = urllib.parse.quote(result["compat_session_id"], safe="")
            with urllib.request.urlopen(
                f"http://{bind_host}:{bind_port}/api/events?session={session_id}&limit=1",
                timeout=4,
            ) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if isinstance(payload, dict) and payload.get("ok") is True:
                result["compat_events"] = True
        except Exception:
            pass

if result["api_ready"] and result["compat_sessions"] and result["compat_remote_sessions"] and (
    result["compat_events"] or not result["compat_session_id"]
):
    result["recommendation"] = "ready"
elif result["service_active"]:
    result["recommendation"] = "service_running_but_api_unreachable"
elif result["service_installed"] and result["port_in_use"] and not result["listener_matches_expected_release"]:
    result["recommendation"] = "legacy_process_conflict"
elif result["service_installed"] and result["port_in_use"]:
    result["recommendation"] = "port_conflict"
elif result["service_installed"]:
    result["recommendation"] = "service_installed_but_inactive"
elif result["port_in_use"] and result["listener_command"]:
    result["recommendation"] = "port_occupied_without_service"
elif result["port_in_use"]:
    result["recommendation"] = "port_occupied_without_service"
elif result["sudo_ok"] and result["python_ok"] and result["curl_ok"] and result["tar_ok"]:
    result["recommendation"] = "bootstrap_recommended"
else:
    result["recommendation"] = "prerequisites_missing"

print(json.dumps({"status": 200, "body": json.dumps(result, ensure_ascii=False)}, ensure_ascii=False))
"""


def parse_bool(value: str, default: bool = True) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def parse_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(minimum, min(maximum, parsed))


def normalize_source_label(record: Any) -> str:
    return describe_source(record.originator, record.source).get("long_label", "")


def parse_session_keys(payload: Dict[str, Any]) -> list[str]:
    raw = payload.get("session_ids")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def as_session_item(record: Any) -> Dict[str, Any]:
    source_info = describe_source(record.originator, record.source)
    display_title = choose_display_title(
        record.vscode_display_name,
        record.thread_name,
        record.title,
        record.session_id,
    )
    try:
        stat = record.path.stat()
        updated_at = iso_from_epoch(stat.st_mtime)
        updated_at_epoch_ms = int(stat.st_mtime * 1000)
    except FileNotFoundError:
        updated_at = ""
        updated_at_epoch_ms = 0
    return {
        "id": record.session_id,
        "alias": record.alias,
        "session_title": record.title,
        "session_title_is_noisy": bool(record.title and title_looks_noisy(record.title)),
        "official_title": record.thread_name,
        "official_title_is_noisy": bool(record.thread_name and title_looks_noisy(record.thread_name)),
        "display_title": display_title,
        "title": record.title,
        "thread_name": record.thread_name,
        "vscode_display_name": record.vscode_display_name,
        "timestamp": record.timestamp,
        "cwd": display_path(record.cwd) if record.cwd else "",
        "cwd_raw": record.cwd,
        "cwd_display": display_path(record.cwd) if record.cwd else "",
        "originator": record.originator,
        "source": record.source,
        "session_source": getattr(record, "session_source", "") or "",
        "client_source": getattr(record, "client_source", "") or "",
        "source_label": source_info.get("long_label", "") or source_info.get("label", "") or "-",
        "source_short_label": source_info.get("label", "") or "-",
        "source_filter_key": source_info.get("filter_key", "") or "",
        "source_description": source_info.get("description", "") or "",
        "parent_session_id": getattr(record, "parent_session_id", "") or "",
        "parent_display_title": getattr(record, "parent_display_title", "") or "",
        "subagent_nickname": getattr(record, "subagent_nickname", "") or "",
        "subagent_role": getattr(record, "subagent_role", "") or "",
        "subagent_depth": int(getattr(record, "subagent_depth", 0) or 0),
        "model": record.model,
        "effort": record.effort,
        "session_size_bytes": int(getattr(record, "session_size_bytes", 0) or 0),
        "archived": bool(record.archived),
        "slack_threads": list(record.slack_threads),
        "updated_at": updated_at,
        "updated_at_epoch_ms": updated_at_epoch_ms,
        "path": display_path(record.path),
        "path_raw": str(record.path),
        "path_display": display_path(record.path),
    }

def build_source_options(app: AppContext, include_archived: bool, query: str = "") -> list[Dict[str, Any]]:
    records = load_records(
        codex_home=app.codex_home,
        include_archived=include_archived,
        slack_db=app.slack_db,
        aliases_db=app.aliases_db,
    )
    if query:
        records = filter_records(records, query, "")
    grouped: Dict[str, Dict[str, Any]] = {}
    for record in records:
        info = describe_source(record.originator, record.source)
        key = str(info.get("filter_key") or "unknown")
        entry = grouped.setdefault(
            key,
            {
                "value": key,
                "label": str(info.get("long_label") or info.get("label") or key),
                "count": 0,
            },
        )
        entry["count"] = int(entry["count"]) + 1
    items = list(grouped.values())
    items.sort(key=lambda item: source_sort_key(str(item.get("value") or ""), str(item.get("label") or "")))
    return items


def load_records_for_view(
    app: AppContext,
    include_archived: bool,
    query: str,
    source_label: str,
    limit: int,
) -> list[Dict[str, Any]]:
    records = load_records(
        codex_home=app.codex_home,
        include_archived=include_archived,
        slack_db=app.slack_db,
        aliases_db=app.aliases_db,
    )
    records = filter_records(records, query, source_label)
    sortable: list[tuple[float, Any]] = []
    for record in records:
        try:
            sortable.append((record.path.stat().st_mtime, record))
        except FileNotFoundError:
            continue
    sortable.sort(key=lambda item: (item[0], item[1].timestamp), reverse=True)
    records = [record for _, record in sortable[:limit]]
    prune_resume_jobs(app)
    items: list[Dict[str, Any]] = []
    for record in records:
        item = as_session_item(record)
        item["progress"] = build_progress_summary(
            record.path,
            remote_running=record.session_id in app.resume_jobs,
            byte_limit=256 * 1024,
            max_lines=400,
        )
        items.append(item)
    return items


def build_stats(app: AppContext, include_archived: bool, query: str, source_label: str) -> Dict[str, Any]:
    records = load_records(
        codex_home=app.codex_home,
        include_archived=include_archived,
        slack_db=app.slack_db,
        aliases_db=app.aliases_db,
    )
    records = filter_records(records, query, source_label)
    originator = Counter(record.originator or "unknown" for record in records)
    source = Counter(describe_source(record.originator, record.source)["long_label"] for record in records)
    model = Counter(record.model or "unknown" for record in records)
    with_alias = sum(1 for record in records if record.alias)
    with_slack = sum(1 for record in records if record.slack_threads)
    return {
        "total": len(records),
        "active": sum(1 for record in records if not record.archived),
        "archived": sum(1 for record in records if record.archived),
        "with_alias": with_alias,
        "with_slack_thread": with_slack,
        "top_originator": originator.most_common(5),
        "top_source": source.most_common(5),
        "top_model": model.most_common(5),
    }


def extract_text_from_content(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for chunk in content:
        if not isinstance(chunk, dict):
            continue
        chunk_type = str(chunk.get("type") or "")
        if chunk_type not in {"input_text", "output_text", "text"}:
            continue
        value = str(chunk.get("text") or "").strip()
        if value:
            texts.append(value)
    return "\n".join(texts)


def display_continue_prompt(prompt: str) -> str:
    value = str(prompt or "").strip()
    if value == DEFAULT_CONTINUE_PROMPT:
        return DEFAULT_CONTINUE_PROMPT_LABEL
    return value


def read_session_history(path: Path, limit: int) -> tuple[list[Dict[str, str]], int]:
    entries: list[Dict[str, str]] = []
    with path.open("r", encoding="utf-8") as file_obj:
        for raw_line in file_obj:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue

            item_type = obj.get("type")
            timestamp = str(obj.get("timestamp") or "")
            payload = obj.get("payload", {})
            if not isinstance(payload, dict):
                continue

            role = ""
            phase = ""
            text = ""
            if item_type == "response_item" and payload.get("type") == "message":
                role = str(payload.get("role") or "").strip()
                if role not in {"user", "assistant"}:
                    continue
                text = extract_text_from_content(payload.get("content"))
                phase = str(payload.get("phase") or "").strip()
            elif item_type == "event_msg" and payload.get("type") == "user_message":
                role = "user"
                text = str(payload.get("message") or "").strip()
                phase = "event"
            else:
                continue

            if role == "user":
                text = display_continue_prompt(text)
            if not text:
                continue
            if entries and entries[-1]["role"] == role and entries[-1]["text"] == text:
                continue
            entries.append(
                {
                    "timestamp": timestamp,
                    "role": role,
                    "phase": phase,
                    "text": text,
                }
            )

    total = len(entries)
    if total > limit:
        entries = entries[-limit:]
    return entries, total


def compact_multiline_text(text: str, *, max_lines: int = 12, max_chars: int = 4000) -> str:
    value = str(text or "").replace("\r\n", "\n").strip()
    if not value:
        return ""
    if len(value) > max_chars:
        value = value[: max_chars - 1].rstrip() + "…"
    lines = value.splitlines()
    if len(lines) > max_lines:
        hidden = len(lines) - max_lines
        value = "\n".join(lines[:max_lines]).rstrip() + f"\n… ({hidden} more lines)"
    return value


def short_event_preview(text: str, *, max_len: int = 180) -> str:
    value = " ".join(str(text or "").strip().split())
    if not value:
        return ""
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def extract_reasoning_preview(payload: Dict[str, Any]) -> str:
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts: list[str] = []
        for item in summary:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
            elif isinstance(item, dict):
                text = str(item.get("text") or item.get("summary") or "").strip()
                if text:
                    parts.append(text)
        if parts:
            return compact_multiline_text("\n".join(parts), max_lines=6, max_chars=1200)
    content = payload.get("content")
    if isinstance(content, str):
        return compact_multiline_text(content, max_lines=6, max_chars=1200)
    return ""


def parse_exec_output(output: str) -> Dict[str, Any]:
    text = str(output or "")
    exit_code: Optional[int] = None
    chunk_id = ""
    command = ""
    body = text
    lines = text.splitlines()
    body_start = 0
    for index, line in enumerate(lines[:8]):
        if line.startswith("Command: "):
            command = line[len("Command: ") :].strip()
            body_start = max(body_start, index + 1)
        elif line.startswith("Chunk ID: "):
            chunk_id = line[len("Chunk ID: ") :].strip()
            body_start = max(body_start, index + 1)
        elif line.startswith("Process exited with code "):
            body_start = max(body_start, index + 1)
            suffix = line[len("Process exited with code ") :].strip()
            try:
                exit_code = int(suffix.split()[0])
            except Exception:
                exit_code = None
        elif line == "Output:":
            body_start = index + 1
            break
    if 0 < body_start <= len(lines):
        body = "\n".join(lines[body_start:]).strip()
    return {
        "command": command,
        "chunk_id": chunk_id,
        "exit_code": exit_code,
        "body": compact_multiline_text(body, max_lines=18, max_chars=5000),
        "preview": short_event_preview(body, max_len=220),
    }


def parse_session_event(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    item_type = str(obj.get("type") or "").strip()
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    timestamp = str(obj.get("timestamp") or "").strip()
    turn_id = str(obj.get("turn_id") or payload.get("turn_id") or "").strip()

    if item_type == "turn_context":
        return {
            "kind": "context",
            "timestamp": timestamp,
            "turn_id": turn_id,
            "title": "Turn context",
            "preview": "Turn context",
            "text": "",
        }

    if item_type == "response_item":
        payload_type = str(payload.get("type") or "").strip()
        role = str(payload.get("role") or "").strip()
        if payload_type == "message":
            text = extract_text_from_content(payload.get("content"))
            if not text:
                return None
            phase = str(payload.get("phase") or "").strip()
            kind = "assistant_message" if role == "assistant" else "user_message"
            display_text = text if role == "assistant" else display_continue_prompt(text)
            return {
                "kind": kind,
                "timestamp": timestamp,
                "turn_id": turn_id,
                "role": role,
                "phase": phase,
                "title": "Assistant" if role == "assistant" else "User",
                "preview": short_event_preview(display_text),
                "text": compact_multiline_text(display_text, max_lines=24, max_chars=7000),
            }
        if payload_type in {"function_call", "custom_tool_call"}:
            tool_name = str(payload.get("name") or payload.get("tool_name") or "").strip() or "tool"
            args_text = compact_multiline_text(str(payload.get("arguments") or ""), max_lines=16, max_chars=3000)
            return {
                "kind": "tool_call",
                "timestamp": timestamp,
                "turn_id": turn_id,
                "tool_name": tool_name,
                "call_id": str(payload.get("call_id") or "").strip(),
                "title": f"Tool call · {tool_name}",
                "preview": tool_name,
                "text": args_text,
            }
        if payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or "").strip()
            parsed = parse_exec_output(str(payload.get("output") or ""))
            title = "Tool output"
            if parsed["command"]:
                title = f"Tool output · {parsed['command']}"
            return {
                "kind": "tool_output",
                "timestamp": timestamp,
                "turn_id": turn_id,
                "call_id": call_id,
                "command": parsed["command"],
                "chunk_id": parsed["chunk_id"],
                "exit_code": parsed["exit_code"],
                "title": title,
                "preview": parsed["preview"] or parsed["command"] or "tool output",
                "text": parsed["body"],
            }
        if payload_type == "reasoning":
            text = extract_reasoning_preview(payload)
            return {
                "kind": "reasoning",
                "timestamp": timestamp,
                "turn_id": turn_id,
                "title": "Reasoning",
                "preview": short_event_preview(text) or "reasoning",
                "text": text,
            }
        if payload_type == "web_search_call":
            action = payload.get("action") or {}
            query = ""
            if isinstance(action, dict):
                query = str(action.get("query") or "").strip()
            status = str(payload.get("status") or "").strip()
            return {
                "kind": "web_search",
                "timestamp": timestamp,
                "turn_id": turn_id,
                "status": status,
                "title": "Web search",
                "preview": short_event_preview(query or status or "web_search"),
                "text": compact_multiline_text(query, max_lines=8, max_chars=1200),
            }
        return None

    if item_type == "event_msg":
        payload_type = str(payload.get("type") or "").strip()
        phase = str(payload.get("phase") or "").strip()
        if payload_type == "user_message":
            text = str(payload.get("message") or "").strip()
            if not text:
                return None
            return {
                "kind": "user_message",
                "timestamp": timestamp,
                "turn_id": turn_id,
                "phase": "event",
                "title": "User",
                "preview": short_event_preview(display_continue_prompt(text)),
                "text": compact_multiline_text(display_continue_prompt(text), max_lines=16, max_chars=3000),
            }
        if payload_type == "agent_message":
            text = str(payload.get("message") or "").strip()
            if not text:
                return None
            return {
                "kind": "commentary",
                "timestamp": timestamp,
                "turn_id": turn_id,
                "phase": phase,
                "title": "Commentary",
                "preview": short_event_preview(text),
                "text": compact_multiline_text(text, max_lines=16, max_chars=3000),
            }
        if payload_type == "task_started":
            return {
                "kind": "task_started",
                "timestamp": timestamp,
                "turn_id": turn_id,
                "title": "Task started",
                "preview": "task started",
                "text": "",
            }
        if payload_type == "task_complete":
            return {
                "kind": "task_complete",
                "timestamp": timestamp,
                "turn_id": turn_id,
                "title": "Task complete",
                "preview": "task complete",
                "text": "",
            }
        if payload_type == "turn_aborted":
            return {
                "kind": "turn_aborted",
                "timestamp": timestamp,
                "turn_id": turn_id,
                "title": "Turn aborted",
                "preview": "turn aborted",
                "text": "",
            }
        if payload_type == "token_count":
            info = payload.get("info") or {}
            preview = ""
            if isinstance(info, dict):
                total_tokens = info.get("total_tokens")
                output_tokens = info.get("output_tokens")
                input_tokens = info.get("input_tokens")
                parts = []
                if input_tokens is not None:
                    parts.append(f"in={input_tokens}")
                if output_tokens is not None:
                    parts.append(f"out={output_tokens}")
                if total_tokens is not None:
                    parts.append(f"total={total_tokens}")
                preview = ", ".join(parts)
            return {
                "kind": "token_count",
                "timestamp": timestamp,
                "turn_id": turn_id,
                "title": "Token count",
                "preview": preview or "token update",
                "text": compact_multiline_text(json.dumps(info, ensure_ascii=False, indent=2), max_lines=18, max_chars=2400) if info else "",
            }
        return None

    return None


def normalized_event_text(event: Dict[str, Any]) -> str:
    text = str(event.get("text") or event.get("preview") or "").strip()
    if not text:
        return ""
    return " ".join(text.split())


def event_duplicate_bucket(event: Dict[str, Any]) -> str:
    kind = str(event.get("kind") or "").strip()
    if kind in {"commentary", "assistant_message"}:
        return "assistant_surface"
    if kind == "user_message":
        return "user_surface"
    return kind


def event_duplicate_priority(event: Dict[str, Any]) -> int:
    kind = str(event.get("kind") or "").strip()
    if kind == "commentary":
        return 30
    if kind == "assistant_message":
        return 20
    if kind == "user_message":
        return 10
    return 0


def events_are_duplicates(previous: Dict[str, Any], current: Dict[str, Any]) -> bool:
    if event_duplicate_bucket(previous) != event_duplicate_bucket(current):
        return False
    previous_turn = str(previous.get("turn_id") or "").strip()
    current_turn = str(current.get("turn_id") or "").strip()
    if previous_turn and current_turn and previous_turn != current_turn:
        return False
    previous_timestamp = str(previous.get("timestamp") or "").strip()[:19]
    current_timestamp = str(current.get("timestamp") or "").strip()[:19]
    if previous_timestamp and current_timestamp and previous_timestamp != current_timestamp:
        return False
    previous_text = normalized_event_text(previous)
    current_text = normalized_event_text(current)
    if not previous_text or not current_text:
        return False
    return previous_text == current_text


def dedupe_adjacent_session_events(events: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    deduped: list[Dict[str, Any]] = []
    for event in events:
        if deduped and events_are_duplicates(deduped[-1], event):
            if event_duplicate_priority(event) > event_duplicate_priority(deduped[-1]):
                deduped[-1] = event
            continue
        deduped.append(event)
    return deduped


def read_recent_session_events(
    path: Path,
    *,
    limit: int = 120,
    byte_limit: int = 2 * 1024 * 1024,
    max_lines: int = 4000,
) -> tuple[list[Dict[str, Any]], int]:
    events: list[Dict[str, Any]] = []
    for raw_line in read_tail_lines(path, byte_limit=byte_limit, max_lines=max_lines):
        try:
            obj = json.loads(raw_line)
        except Exception:
            continue
        event = parse_session_event(obj)
        if event:
            events.append(event)
    events = dedupe_adjacent_session_events(events)
    if limit > 0 and len(events) > limit:
        events = events[-limit:]
    try:
        cursor = path.stat().st_size
    except FileNotFoundError:
        cursor = 0
    return events, cursor


def read_session_events_since(
    path: Path,
    *,
    cursor: int,
    limit: int = 200,
    byte_limit: int = 512 * 1024,
) -> tuple[list[Dict[str, Any]], int, bool]:
    events: list[Dict[str, Any]] = []
    reset = False
    try:
        with path.open("rb") as file_obj:
            file_obj.seek(0, os.SEEK_END)
            size = file_obj.tell()
            if cursor < 0 or cursor > size:
                reset = True
                events, new_cursor = read_recent_session_events(path, limit=limit)
                return events, new_cursor, reset
            start = cursor
            file_obj.seek(start)
            raw = file_obj.read(byte_limit + 1)
            if not raw:
                return [], size, reset
            if len(raw) > byte_limit:
                raw = raw[:byte_limit]
            new_cursor = start + len(raw)
            lines = raw.splitlines()
            if raw and not raw.endswith(b"\n"):
                if lines:
                    lines = lines[:-1]
                    new_cursor = start + sum(len(line) + 1 for line in lines)
                else:
                    new_cursor = start
            for line in lines:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                event = parse_session_event(obj)
                if event:
                    events.append(event)
            events = dedupe_adjacent_session_events(events)
            if limit > 0 and len(events) > limit:
                events = events[-limit:]
            return events, max(new_cursor, cursor), reset
    except FileNotFoundError:
        return [], 0, True
    except Exception:
        return [], cursor, reset

def read_session_rounds(path: Path, rounds: int) -> tuple[list[Dict[str, str]], int]:
    entries, _ = read_session_history(path, limit=200_000)
    grouped: list[list[Dict[str, str]]] = []
    current: list[Dict[str, str]] = []

    for entry in entries:
        role = str(entry.get("role") or "").strip()
        if role == "user":
            if current and any(str(item.get("role") or "").strip() == "assistant" for item in current):
                grouped.append(current)
                current = [entry]
                continue
            current.append(entry)
            continue
        if not current:
            current = [entry]
            continue
        current.append(entry)

    if current:
        grouped.append(current)

    total_rounds = len(grouped)
    if total_rounds > rounds:
        grouped = grouped[-rounds:]
    flattened = [item for group in grouped for item in group]
    return flattened, total_rounds


def iso_from_epoch(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compact_preview(text: str, limit: int = 180) -> str:
    cleaned = " ".join(str(text or "").replace("\r\n", "\n").replace("\r", "\n").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = 310_000) -> str:
    actual_salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), actual_salt, iterations)
    return f"pbkdf2_sha256${iterations}${b64url_encode(actual_salt)}${b64url_encode(derived)}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations_text, salt_text, derived_text = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = b64url_decode(salt_text)
        expected = b64url_decode(derived_text)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def default_auth_file() -> Path:
    return Path.home() / ".config" / "codex-sessions-web" / "auth.json"


def load_auth_config(path: Path) -> Optional[AuthConfig]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return None
    data = json.loads(resolved.read_text(encoding="utf-8"))
    password_hash = str(data.get("password_hash") or "").strip()
    session_secret_text = str(data.get("session_secret") or "").strip()
    if not password_hash or not session_secret_text:
        raise ValueError(f"Invalid auth config: {display_path(resolved)}")
    cookie_name = str(data.get("cookie_name") or "codex_sessions_web").strip() or "codex_sessions_web"
    ttl = int(data.get("session_ttl_seconds") or 30 * 24 * 3600)
    return AuthConfig(
        file_path=resolved,
        password_hash=password_hash,
        session_secret=b64url_decode(session_secret_text),
        cookie_name=cookie_name,
        session_ttl_seconds=max(3600, ttl),
    )


def write_auth_config(path: Path, password: str) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": iso_now(),
        "password_hash": hash_password(password),
        "session_secret": b64url_encode(secrets.token_bytes(32)),
        "cookie_name": "codex_sessions_web",
        "session_ttl_seconds": 30 * 24 * 3600,
    }
    resolved.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(resolved, 0o600)
    return resolved


def make_session_cookie(secret: bytes, *, csrf_token: str, ttl_seconds: int) -> str:
    payload = {
        "csrf": csrf_token,
        "exp": int(time.time()) + ttl_seconds,
        "iat": int(time.time()),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret, raw, hashlib.sha256).digest()
    return f"{b64url_encode(raw)}.{b64url_encode(signature)}"


def parse_session_cookie(secret: bytes, cookie_value: str) -> Optional[Dict[str, Any]]:
    try:
        encoded_payload, encoded_sig = cookie_value.split(".", 1)
        raw = b64url_decode(encoded_payload)
        actual_sig = b64url_decode(encoded_sig)
        expected_sig = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(actual_sig, expected_sig):
            return None
        payload = json.loads(raw.decode("utf-8"))
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
        if not str(payload.get("csrf") or "").strip():
            return None
        return payload
    except Exception:
        return None


def load_remote_marks(path: Path) -> Dict[str, RemoteMark]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    marks: Dict[str, RemoteMark] = {}
    for session_id, payload in data.items():
        if not isinstance(payload, dict):
            continue
        key = str(session_id or "").strip()
        started_at = str(payload.get("started_at") or "").strip()
        prompt = str(payload.get("prompt") or "").strip()
        log_path = str(payload.get("log_path") or "").strip()
        if not key or not started_at:
            continue
        marks[key] = RemoteMark(
            session_id=key,
            started_at=started_at,
            prompt=prompt,
            log_path=log_path,
        )
    return marks


def save_remote_marks(path: Path, marks: Dict[str, RemoteMark]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        session_id: {
            "started_at": mark.started_at,
            "prompt": mark.prompt,
            "log_path": mark.log_path,
        }
        for session_id, mark in sorted(marks.items())
    }
    resolved.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(resolved, 0o600)


def load_remote_watchlist(path: Path) -> Dict[str, RemoteWatch]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    items: Dict[str, RemoteWatch] = {}
    for session_id, payload in data.items():
        key = str(session_id or "").strip()
        if not key:
            continue
        added_at = ""
        auto_continue = False
        continue_prompt = ""
        last_resumed_turn_id = ""
        last_resumed_at = ""
        if isinstance(payload, dict):
            added_at = str(payload.get("added_at") or "").strip()
            auto_continue = parse_bool(str(payload.get("auto_continue") or ""), default=False)
            continue_prompt = str(payload.get("continue_prompt") or "").strip()
            last_resumed_turn_id = str(payload.get("last_resumed_turn_id") or "").strip()
            last_resumed_at = str(payload.get("last_resumed_at") or "").strip()
        elif isinstance(payload, str):
            added_at = str(payload).strip()
        items[key] = RemoteWatch(
            session_id=key,
            added_at=added_at,
            auto_continue=auto_continue,
            continue_prompt=continue_prompt,
            last_resumed_turn_id=last_resumed_turn_id,
            last_resumed_at=last_resumed_at,
        )
    return items


def save_remote_watchlist(path: Path, items: Dict[str, RemoteWatch]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        session_id: {
            "added_at": str(item.added_at or "").strip(),
            "auto_continue": bool(item.auto_continue),
            "continue_prompt": str(item.continue_prompt or "").strip(),
            "last_resumed_turn_id": str(item.last_resumed_turn_id or "").strip(),
            "last_resumed_at": str(item.last_resumed_at or "").strip(),
        }
        for session_id, item in sorted(items.items())
    }
    resolved.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(resolved, 0o600)


def count_auto_continue_watches(items: Dict[str, RemoteWatch]) -> int:
    return sum(1 for item in items.values() if bool(item.auto_continue))


def default_targets_file() -> Path:
    return Path.home() / ".config" / "codex-sessions-web" / "targets.json"


def build_local_target() -> MachineTarget:
    try:
        host = str(os.uname().nodename or "").strip() or "localhost"
    except Exception:
        host = "localhost"
    return MachineTarget(
        target_id=LOCAL_TARGET_ID,
        label=f"本机 ({host})",
        kind="local",
        ssh_host="",
        ssh_user="",
        ssh_port=22,
        base_url="http://127.0.0.1:8765",
        auth_mode="key",
    )


def normalize_target_id(value: str) -> str:
    text = str(value or "").strip()
    return text or LOCAL_TARGET_ID


def make_target_id(*, ssh_user: str, ssh_host: str, ssh_port: int) -> str:
    user = str(ssh_user or "").strip().lower()
    host = str(ssh_host or "").strip().lower()
    port = int(ssh_port or 22)
    return f"{user}@{host}:{port}"


def normalize_target_auth_mode(value: str) -> str:
    text = str(value or "").strip().lower()
    return "password" if text == "password" else "key"


def target_to_public_dict(target: MachineTarget) -> Dict[str, Any]:
    return {
        "id": target.target_id,
        "label": target.label,
        "kind": target.kind,
        "ssh_host": target.ssh_host,
        "ssh_user": target.ssh_user,
        "ssh_port": int(target.ssh_port or 22),
        "base_url": target.base_url,
        "auth_mode": normalize_target_auth_mode(target.auth_mode),
    }


def parse_target_base_url(base_url: str) -> tuple[str, int]:
    text = str(base_url or "").strip() or DEFAULT_TARGET_BASE_URL
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base_url must start with http:// or https://")
    host = str(parsed.hostname or "").strip()
    if not host:
        raise ValueError("base_url is missing a hostname")
    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    return host, port


def build_target_from_payload(payload: Dict[str, Any]) -> MachineTarget:
    label = str(payload.get("label") or "").strip()
    ssh_host = str(payload.get("ssh_host") or payload.get("host") or "").strip()
    ssh_user = str(payload.get("ssh_user") or payload.get("user") or "").strip()
    try:
        ssh_port = int(payload.get("ssh_port") or payload.get("port") or 22)
    except Exception:
        ssh_port = 22
    ssh_port = max(1, min(65535, ssh_port))
    base_url = str(payload.get("base_url") or DEFAULT_TARGET_BASE_URL).strip() or DEFAULT_TARGET_BASE_URL
    auth_mode = normalize_target_auth_mode(str(payload.get("auth_mode") or payload.get("ssh_auth") or "key"))
    ssh_password = str(payload.get("ssh_password") or "")
    if not ssh_host:
        raise ValueError("Missing `ssh_host`")
    if not ssh_user:
        raise ValueError("Missing `ssh_user`")
    target_id = make_target_id(ssh_user=ssh_user, ssh_host=ssh_host, ssh_port=ssh_port)
    return MachineTarget(
        target_id=target_id,
        label=label or target_id,
        kind="ssh",
        ssh_host=ssh_host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        base_url=base_url,
        auth_mode=auth_mode,
        ssh_password=ssh_password,
    )


def enrich_target_check_result(result: Dict[str, Any], repo_root: Path) -> Dict[str, Any]:
    local_release = build_release_metadata(repo_root)
    remote_release = result.get("release_metadata")
    result["local_release_metadata"] = local_release
    result["local_version_label"] = version_label(local_release)
    result["remote_version_label"] = version_label(remote_release)
    version_match = compare_release_metadata(local_release, remote_release)
    result["version_match"] = version_match
    is_compatible = bool(result.get("compat_sessions")) and bool(result.get("compat_remote_sessions")) and (
        bool(result.get("compat_events")) or not str(result.get("compat_session_id") or "").strip()
    )
    result["compat_ready"] = is_compatible
    if str(result.get("recommendation") or "") in {"legacy_process_conflict", "port_conflict", "port_occupied_without_service"}:
        return result
    if result.get("api_ready") and is_compatible:
        if version_match is True:
            result["recommendation"] = "ready"
        elif version_match is False:
            result["recommendation"] = "update_recommended"
        else:
            result["recommendation"] = "ready_unknown_version"
    elif result.get("api_ready"):
        result["recommendation"] = "upgrade_required_for_ui"
    return result


def load_machine_targets(path: Path) -> Dict[str, MachineTarget]:
    resolved = path.expanduser().resolve()
    targets: Dict[str, MachineTarget] = {LOCAL_TARGET_ID: build_local_target()}
    if not resolved.exists():
        return targets
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return targets
    raw_items = data.get("targets") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        return targets
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        ssh_host = str(item.get("ssh_host") or "").strip()
        ssh_user = str(item.get("ssh_user") or "").strip()
        if not ssh_host or not ssh_user:
            continue
        try:
            ssh_port = int(item.get("ssh_port") or 22)
        except Exception:
            ssh_port = 22
        ssh_port = max(1, min(65535, ssh_port))
        target_id = str(item.get("id") or "").strip() or make_target_id(
            ssh_user=ssh_user,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
        )
        targets[target_id] = MachineTarget(
            target_id=target_id,
            label=str(item.get("label") or target_id).strip() or target_id,
            kind="ssh",
            ssh_host=ssh_host,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            base_url=str(item.get("base_url") or "http://127.0.0.1:8765").strip() or "http://127.0.0.1:8765",
            auth_mode=normalize_target_auth_mode(str(item.get("auth_mode") or "key")),
        )
    return targets


def save_machine_targets(path: Path, targets: Dict[str, MachineTarget]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "targets": [
            {
                "id": target.target_id,
                "label": target.label,
                "ssh_host": target.ssh_host,
                "ssh_user": target.ssh_user,
                "ssh_port": int(target.ssh_port or 22),
                "base_url": target.base_url,
                "auth_mode": normalize_target_auth_mode(target.auth_mode),
            }
            for target in sorted(targets.values(), key=lambda item: (item.kind != "local", item.label.lower(), item.target_id))
            if target.kind != "local"
        ]
    }
    resolved.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(resolved, 0o600)


def build_forwarded_api_path(parsed: Any) -> str:
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key in ("target", "target_label", "ssh_host", "ssh_user", "ssh_port", "base_url", "ssh_auth"):
        query.pop(key, None)
    query_parts: list[tuple[str, str]] = []
    for key, values in query.items():
        for value in values:
            query_parts.append((str(key), str(value)))
    encoded = urlencode(query_parts, doseq=True)
    return parsed.path + (f"?{encoded}" if encoded else "")


def sanitize_next_path(value: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("/") or text.startswith("//"):
        return "/"
    parsed = urlparse(text)
    if parsed.scheme or parsed.netloc:
        return "/"
    cleaned = parsed.path or "/"
    if parsed.query:
        cleaned += f"?{parsed.query}"
    return cleaned


def generate_password(length: int = 24) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*()-_=+"
    return "".join(secrets.choice(alphabet) for _ in range(max(16, length)))


def validate_password(password: str) -> None:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")


def client_ip_from_headers(handler: BaseHTTPRequestHandler) -> str:
    cf_ip = str(handler.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        return cf_ip
    forwarded = str(handler.headers.get("X-Forwarded-For") or "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return str(handler.client_address[0] if handler.client_address else "unknown")


def auth_cooldown_seconds(failure_count: int) -> int:
    if failure_count >= 12:
        return 30 * 60
    if failure_count >= 8:
        return 10 * 60
    if failure_count >= 5:
        return 2 * 60
    if failure_count >= 3:
        return 20
    return 0


def prune_auth_failures(app: AppContext, *, max_age_seconds: int = 24 * 3600) -> None:
    now = time.time()
    for key, state in list(app.auth_failures.items()):
        if state.blocked_until > now:
            continue
        if now - state.last_attempt > max_age_seconds:
            app.auth_failures.pop(key, None)


def read_tail_lines(path: Path, *, byte_limit: int = 512 * 1024, max_lines: int = 400) -> list[str]:
    try:
        with path.open("rb") as file_obj:
            file_obj.seek(0, os.SEEK_END)
            size = file_obj.tell()
            start = max(0, size - byte_limit)
            file_obj.seek(start)
            raw = file_obj.read()
    except Exception:
        return []

    if start > 0:
        newline_index = raw.find(b"\n")
        if newline_index >= 0:
            raw = raw[newline_index + 1 :]

    lines = raw.splitlines()
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return [line.decode("utf-8", errors="replace") for line in lines if line.strip()]


def recent_unique(items: list[str], *, limit: int = 3) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in reversed(items):
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def append_unique_text(items: list[str], text: str) -> None:
    value = str(text or "").strip()
    if not value:
        return
    if items and items[-1] == value:
        return
    items.append(value)


def task_turn_is_open(markers: list[str]) -> bool:
    last_started = -1
    last_settled = -1
    for index, marker in enumerate(markers):
        if marker.startswith("event:task_started"):
            last_started = index
        elif marker.startswith("event:task_complete") or marker.startswith("event:turn_aborted"):
            last_settled = index
    return last_started > last_settled


def infer_progress_state(markers: list[str], remote_running: bool) -> tuple[str, str]:
    if remote_running:
        return "running", "网页触发的继续任务仍在运行"
    if not markers:
        return "unknown", "最近尾部没有可识别事件"

    last = markers[-1]
    turn_open = task_turn_is_open(markers)
    if last.startswith("event:task_complete"):
        return "waiting", "上一轮已完成，通常正在等你下一句"
    if last.startswith("event:turn_aborted"):
        return "aborted", "上一轮被中断"
    if last in {"event:user_message:", "response:message:user"}:
        return "queued", "最新输入已写入，正在等待 agent 继续处理"
    if last.startswith("response:function_call") or last.startswith("response:custom_tool_call"):
        return "running", "最近还在发起工具调用"
    if last.startswith("response:function_call_output") or last.startswith("response:custom_tool_call_output"):
        return "running", "最近刚收到工具输出"
    if last == "response:reasoning:" or last == "turn_context" or last.startswith("event:task_started"):
        return "running", "最近还在推理或准备下一轮"
    if last.startswith("response:message:assistant") or last.startswith("event:agent_message:"):
        if turn_open:
            return "running", "当前轮次尚未 task_complete，助手仍在分段输出或继续处理"
        return "waiting", "最新是助手输出，通常已到可继续状态"
    if turn_open:
        return "running", "当前轮次尚未 task_complete，agent 仍在处理中"
    return "unknown", f"最新事件: {last}"


def contains_any(text: str, needles: list[str]) -> bool:
    lowered = str(text or "").lower()
    return any(needle in lowered for needle in needles)


def infer_attention_state(
    *,
    progress_state: str,
    assistant_phase: str,
    assistant_text: str,
    recent_markers: list[str],
    remote_running: bool,
) -> tuple[str, str]:
    if remote_running or progress_state in {"running", "queued"}:
        return "active", "任务仍在自动推进，暂时不该重复发消息"
    if progress_state == "aborted":
        return "needs_attention", "上一轮中断了，通常需要人工判断是否重试"

    phase = str(assistant_phase or "").strip().lower()
    text = str(assistant_text or "").strip()
    if phase == "final_answer":
        return "completed", "最新回复已进入 final answer，通常表示这一轮已经交付结果"
    if contains_any(
        text,
        [
            "需要你",
            "需要你来",
            "需要人工",
            "请确认",
            "请提供",
            "请先",
            "你决定",
            "无法继续",
            "卡住",
            "受阻",
            "报错",
            "失败",
            "blocked",
            "need your",
            "please provide",
            "please confirm",
            "i need",
            "can't continue",
            "cannot continue",
            "manual",
        ],
    ) or "？" in text or "?" in text:
        return "needs_attention", "最新输出像是在请求信息、确认或人工介入"
    if progress_state == "waiting":
        if any(marker.startswith("event:task_complete") for marker in recent_markers):
            return "completed", "最近记录到 task_complete，这一轮大概率已经做完"
        return "check", "当前已停下，但是否算完成还需要你看一眼摘要"
    if phase == "commentary":
        return "needs_attention", "最新停在 commentary 阶段，更像是在等人工判断而不是正式收口"
    return "unknown", "当前信号不足，还不能稳定判断是完成还是需要介入"


def build_progress_summary(
    path: Path,
    *,
    remote_running: bool,
    byte_limit: int = 2 * 1024 * 1024,
    max_lines: int = 2000,
) -> Dict[str, Any]:
    tail_lines = read_tail_lines(path, byte_limit=byte_limit, max_lines=max_lines)
    last_timestamp = ""
    last_assistant = ""
    last_assistant_phase = ""
    last_user = ""
    assistant_segments: list[str] = []
    tool_names: list[str] = []
    markers: list[str] = []

    for raw_line in tail_lines:
        try:
            obj = json.loads(raw_line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue

        timestamp = str(obj.get("timestamp") or "").strip()
        if timestamp:
            last_timestamp = timestamp

        item_type = str(obj.get("type") or "")
        payload = obj.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        marker = ""
        if item_type == "turn_context":
            marker = "turn_context"
        elif item_type == "response_item":
            payload_type = str(payload.get("type") or "").strip()
            role = str(payload.get("role") or "").strip()
            marker = f"response:{payload_type}:{role}"
            if payload_type == "message":
                text = extract_text_from_content(payload.get("content"))
                if role == "assistant" and text:
                    last_assistant = text
                    last_assistant_phase = str(payload.get("phase") or "").strip()
                    append_unique_text(assistant_segments, text)
                elif role == "user" and text:
                    last_user = display_continue_prompt(text)
                    assistant_segments = []
                    last_assistant = ""
                    last_assistant_phase = ""
            elif payload_type in {"function_call", "custom_tool_call"}:
                tool_name = str(payload.get("name") or payload.get("tool_name") or "").strip()
                if tool_name:
                    tool_names.append(tool_name)
        elif item_type == "event_msg":
            payload_type = str(payload.get("type") or "").strip()
            phase = str(payload.get("phase") or "").strip()
            marker = f"event:{payload_type}:{phase}"
            if payload_type == "user_message":
                text = str(payload.get("message") or "").strip()
                if text:
                    last_user = display_continue_prompt(text)
                    assistant_segments = []
                    last_assistant = ""
                    last_assistant_phase = ""
            elif payload_type == "agent_message":
                text = str(payload.get("message") or "").strip()
                if text:
                    if not last_assistant:
                        last_assistant = text
                        last_assistant_phase = phase
                    append_unique_text(assistant_segments, text)

        if marker and not marker.startswith("event:token_count"):
            markers.append(marker)

    state, reason = infer_progress_state(markers, remote_running)
    # Show the newest fragment first so long in-flight replies stay actionable
    # near the top of the card instead of pushing the latest text below the fold.
    assistant_text = "\n\n".join(reversed(assistant_segments)).strip() or last_assistant
    assistant_preview = compact_preview(assistant_text)
    user_preview = compact_preview(last_user)
    recent_tools = recent_unique(tool_names, limit=3)
    preview_source = assistant_text or last_user or ("最近工具: " + ", ".join(recent_tools) if recent_tools else "")
    preview = compact_preview(preview_source, limit=220)
    recent_markers = markers[-4:]
    attention_state, attention_reason = infer_attention_state(
        progress_state=state,
        assistant_phase=last_assistant_phase,
        assistant_text=assistant_text,
        recent_markers=recent_markers,
        remote_running=remote_running,
    )
    return {
        "state": state,
        "reason": reason,
        "attention_state": attention_state,
        "attention_reason": attention_reason,
        "last_timestamp": last_timestamp,
        "last_assistant_text": assistant_text,
        "last_assistant_preview": assistant_preview,
        "last_assistant_phase": last_assistant_phase,
        "last_user_preview": user_preview,
        "assistant_segment_count": len(assistant_segments),
        "recent_tools": recent_tools,
        "preview": preview,
        "recent_markers": recent_markers,
        "remote_running": remote_running,
    }


def build_turn_signature(turn_id: str, line_number: int) -> str:
    value = str(turn_id or "").strip()
    if value:
        return value
    return f"line:{line_number}"


def inspect_recent_turn_lifecycle(
    path: Path,
    *,
    byte_limit: int = 2 * 1024 * 1024,
    max_lines: int = 4000,
) -> Dict[str, Any]:
    latest_started: Optional[Dict[str, Any]] = None
    latest_settled: Optional[Dict[str, Any]] = None
    latest_completed: Optional[Dict[str, Any]] = None
    latest_aborted: Optional[Dict[str, Any]] = None

    for line_number, raw_line in enumerate(read_tail_lines(path, byte_limit=byte_limit, max_lines=max_lines), start=1):
        try:
            obj = json.loads(raw_line)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "event_msg":
            continue
        payload = obj.get("payload", {})
        if not isinstance(payload, dict):
            continue
        payload_type = str(payload.get("type") or "").strip()
        turn_id = str(payload.get("turn_id") or obj.get("turn_id") or "").strip()
        item = {
            "kind": payload_type,
            "turn_id": turn_id,
            "signature": build_turn_signature(turn_id, line_number),
            "line_number": line_number,
        }
        if payload_type == "task_started":
            latest_started = item
        elif payload_type == "task_complete":
            latest_completed = item
            latest_settled = item
        elif payload_type in {"task_aborted", "turn_aborted"}:
            latest_aborted = item
            latest_settled = item

    turn_open = False
    if latest_started and latest_settled:
        turn_open = int(latest_started["line_number"]) > int(latest_settled["line_number"])
    elif latest_started and not latest_settled:
        turn_open = True

    return {
        "latest_started": latest_started,
        "latest_settled": latest_settled,
        "latest_completed": latest_completed,
        "latest_aborted": latest_aborted,
        "turn_open": turn_open,
    }


def launch_resume_for_record(
    app: AppContext,
    record: Any,
    *,
    prompt: str,
    origin_label: str,
) -> tuple[Optional[ResumeLaunch], Optional[Path], Optional[Path], Optional[int], Optional[str]]:
    prune_resume_jobs(app)
    existing = app.resume_jobs.get(record.session_id)
    if existing and existing.process.poll() is None:
        return existing, None, None, None, "A remote-triggered resume is already running for this session"

    cwd_path = Path(str(record.cwd or "").strip() or str(Path.home())).expanduser()
    if not cwd_path.is_dir():
        cwd_path = Path.home()

    log_dir = app.codex_home / "web_resume_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    started_at = iso_now()
    safe_origin = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(origin_label or "web")).strip("_") or "web"
    log_path = log_dir / f"{started_at.replace(':', '-')}--{safe_origin}--{record.session_id}.log"
    log_handle = log_path.open("ab")
    cmd = [
        app.codex_bin,
        "exec",
        "resume",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--all",
        record.session_id,
        prompt,
    ]
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd_path),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as exc:
        log_handle.close()
        return None, cwd_path, log_path, None, f"Failed to launch resume command: {exc}"

    time.sleep(0.2)
    exit_code = process.poll()
    if exit_code is not None:
        log_handle.flush()
        log_handle.close()
        try:
            error_text = log_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            error_text = ""
        return None, cwd_path, log_path, exit_code, compact_preview(error_text or f"Resume command exited immediately with code {exit_code}", 500)

    launch = ResumeLaunch(
        session_id=record.session_id,
        prompt=prompt,
        started_at=started_at,
        log_path=log_path,
        process=process,
        log_handle=log_handle,
    )
    app.resume_jobs[record.session_id] = launch
    app.remote_marks[record.session_id] = RemoteMark(
        session_id=record.session_id,
        started_at=started_at,
        prompt=display_continue_prompt(prompt),
        log_path=str(log_path),
    )
    save_remote_marks(app.remote_marks_path, app.remote_marks)
    return launch, cwd_path, log_path, None, None


def auto_continue_tick(app: AppContext) -> None:
    if not app.supervisor_lock_active:
        return
    with app.lock:
        if not app.remote_watchlist:
            return
        prune_resume_jobs(app)
        watched = {session_id: item for session_id, item in app.remote_watchlist.items() if item.auto_continue}
        if not watched:
            return
        records = load_records(
            codex_home=app.codex_home,
            include_archived=False,
            slack_db=app.slack_db,
            aliases_db=app.aliases_db,
        )
        by_session = {record.session_id: record for record in records}
        dirty = False
        for session_id in list(app.remote_watchlist):
            if session_id in by_session:
                continue
            watch = app.remote_watchlist.get(session_id)
            if watch is None:
                continue
            if watch.auto_continue:
                watch.auto_continue = False
                dirty = True
        if dirty:
            save_remote_watchlist(app.remote_watchlist_path, app.remote_watchlist)

        for session_id, watch in watched.items():
            record = by_session.get(session_id)
            if record is None:
                continue
            if session_id in app.resume_jobs:
                continue
            lifecycle = inspect_recent_turn_lifecycle(record.path)
            latest_settled = lifecycle.get("latest_settled") or {}
            if lifecycle.get("turn_open"):
                continue
            if str(latest_settled.get("kind") or "") != "task_complete":
                continue
            latest_signature = str(latest_settled.get("signature") or "").strip()
            if not latest_signature or latest_signature == str(watch.last_resumed_turn_id or "").strip():
                continue
            prompt = str(watch.continue_prompt or AUTO_CONTINUE_PROMPT).strip() or AUTO_CONTINUE_PROMPT
            launch, _, _, _, error = launch_resume_for_record(
                app,
                record,
                prompt=prompt,
                origin_label="supervisor",
            )
            if error:
                print(f"[auto-continue] session={session_id} launch failed: {error}", flush=True)
                continue
            if launch is None:
                continue
            watch.last_resumed_turn_id = latest_signature
            watch.last_resumed_at = iso_now()
            save_remote_watchlist(app.remote_watchlist_path, app.remote_watchlist)
            print(
                f"[auto-continue] session={session_id} resumed from completed turn={latest_signature}",
                flush=True,
            )


def auto_continue_loop(app: AppContext, interval_seconds: int = AUTO_CONTINUE_INTERVAL_SECONDS) -> None:
    while not app.shutdown_event.is_set():
        if not app.supervisor_lock_active:
            try:
                handle = app.supervisor_lock_path.open("a+")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle.seek(0)
                handle.truncate(0)
                handle.write(f"{os.getpid()}\n")
                handle.flush()
                app.supervisor_lock_handle = handle
                app.supervisor_lock_active = True
                print(
                    f"[auto-continue] acquired supervisor lock {display_path(app.supervisor_lock_path)}",
                    flush=True,
                )
            except BlockingIOError:
                app.supervisor_lock_active = False
                if app.supervisor_lock_handle is not None:
                    try:
                        app.supervisor_lock_handle.close()
                    except Exception:
                        pass
                    app.supervisor_lock_handle = None
                if app.shutdown_event.wait(min(interval_seconds, 10)):
                    break
                continue
            except Exception as exc:
                app.supervisor_lock_active = False
                if app.supervisor_lock_handle is not None:
                    try:
                        app.supervisor_lock_handle.close()
                    except Exception:
                        pass
                    app.supervisor_lock_handle = None
                print(f"[auto-continue] supervisor lock setup failed: {exc}", flush=True)
                if app.shutdown_event.wait(min(interval_seconds, 30)):
                    break
                continue
        try:
            auto_continue_tick(app)
        except Exception as exc:
            print(f"[auto-continue] supervisor tick failed: {exc}", flush=True)
        if app.shutdown_event.wait(interval_seconds):
            break
    if app.supervisor_lock_handle is not None:
        try:
            fcntl.flock(app.supervisor_lock_handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            app.supervisor_lock_handle.close()
        except Exception:
            pass
        app.supervisor_lock_handle = None
    app.supervisor_lock_active = False


def close_resume_job(app: AppContext, session_id: str) -> Optional[ResumeLaunch]:
    job = app.resume_jobs.pop(session_id, None)
    if job is None:
        return None
    try:
        job.log_handle.flush()
    except Exception:
        pass
    try:
        job.log_handle.close()
    except Exception:
        pass
    return job


def clear_remote_mark(app: AppContext, session_id: str) -> None:
    if app.remote_marks.pop(session_id, None) is not None:
        save_remote_marks(app.remote_marks_path, app.remote_marks)


def prune_resume_jobs(app: AppContext) -> None:
    for session_id, job in list(app.resume_jobs.items()):
        if job.process.poll() is None:
            continue
        close_resume_job(app, session_id)


def stop_resume_job(app: AppContext, session_id: str) -> tuple[Optional[ResumeLaunch], Optional[str], Optional[int]]:
    prune_resume_jobs(app)
    job = app.resume_jobs.get(session_id)
    if job is None:
        return None, "No running remote-triggered resume for this session", None

    try:
        job.log_handle.write(f"\n[{iso_now()}] remote stop requested from web UI\n".encode("utf-8"))
        job.log_handle.flush()
    except Exception:
        pass

    try:
        pgid = os.getpgid(job.process.pid)
    except ProcessLookupError:
        pgid = None
    except Exception as exc:
        return job, f"Failed to inspect remote job process group: {exc}", None

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            job.process.terminate()
    except ProcessLookupError:
        pass
    except Exception as exc:
        return job, f"Failed to stop remote job: {exc}", None

    deadline = time.time() + 3.0
    while time.time() < deadline and job.process.poll() is None:
        time.sleep(0.05)

    if job.process.poll() is None:
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
            else:
                job.process.kill()
        except ProcessLookupError:
            pass
        except Exception as exc:
            return job, f"Failed to force-stop remote job: {exc}", None

        deadline = time.time() + 1.0
        while time.time() < deadline and job.process.poll() is None:
            time.sleep(0.05)

    exit_code = job.process.poll()
    close_resume_job(app, session_id)
    clear_remote_mark(app, session_id)
    return job, None, exit_code


def build_remote_items(app: AppContext, *, query: str, limit: int) -> list[Dict[str, Any]]:
    all_records = load_records(
        codex_home=app.codex_home,
        include_archived=False,
        slack_db=app.slack_db,
        aliases_db=app.aliases_db,
    )
    records = filter_records(all_records, query, "")
    sortable: list[tuple[float, Any]] = []
    by_session: Dict[str, Any] = {}
    mtime_by_session: Dict[str, float] = {}
    for record in all_records:
        by_session[record.session_id] = record
    for record in records:
        try:
            mtime = record.path.stat().st_mtime
        except FileNotFoundError:
            continue
        sortable.append((mtime, record))
        mtime_by_session[record.session_id] = mtime
    sortable.sort(key=lambda item: (item[0], item[1].timestamp), reverse=True)
    prune_resume_jobs(app)

    removed_watch = False
    for session_id in list(app.remote_watchlist):
        if session_id in by_session:
            continue
        app.remote_watchlist.pop(session_id, None)
        removed_watch = True
    if removed_watch:
        save_remote_watchlist(app.remote_watchlist_path, app.remote_watchlist)

    watched_ids = set(app.remote_watchlist)
    selected: list[Any] = []
    seen: set[str] = set()
    for _, record in sortable:
        if record.session_id not in watched_ids or record.session_id in seen:
            continue
        selected.append(record)
        seen.add(record.session_id)

    recent_added = 0
    for _, record in sortable:
        if record.session_id in seen:
            continue
        if recent_added >= limit:
            break
        selected.append(record)
        seen.add(record.session_id)
        recent_added += 1

    selected.sort(
        key=lambda record: (
            1 if record.session_id in watched_ids else 0,
            mtime_by_session.get(record.session_id, 0.0),
            str(record.timestamp or ""),
        ),
        reverse=True,
    )

    items: list[Dict[str, Any]] = []
    for record in selected:
        try:
            stat = record.path.stat()
        except FileNotFoundError:
            continue
        item = as_session_item(record)
        item["updated_at"] = iso_from_epoch(stat.st_mtime)
        item["updated_at_epoch_ms"] = int(stat.st_mtime * 1000)
        item["progress"] = build_progress_summary(
            record.path,
            remote_running=record.session_id in app.resume_jobs,
        )
        watch = app.remote_watchlist.get(record.session_id)
        item["watched"] = bool(watch)
        item["watched_at"] = str(watch.added_at or "").strip() if watch else ""
        item["auto_continue"] = bool(watch.auto_continue) if watch else False
        item["auto_continue_prompt"] = str(watch.continue_prompt or AUTO_CONTINUE_PROMPT).strip() if watch else AUTO_CONTINUE_PROMPT
        item["auto_continue_last_resumed_turn_id"] = str(watch.last_resumed_turn_id or "").strip() if watch else ""
        item["auto_continue_last_resumed_at"] = str(watch.last_resumed_at or "").strip() if watch else ""
        items.append(item)
    return items


def build_remote_guard_items(app: AppContext) -> list[Dict[str, Any]]:
    prune_resume_jobs(app)
    if not app.remote_marks:
        return []

    records = load_records(
        codex_home=app.codex_home,
        include_archived=True,
        slack_db=app.slack_db,
        aliases_db=app.aliases_db,
    )
    by_session = {record.session_id: record for record in records}
    items: list[Dict[str, Any]] = []
    removed = False

    for session_id, mark in list(app.remote_marks.items()):
        record = by_session.get(session_id)
        if record is None:
            app.remote_marks.pop(session_id, None)
            removed = True
            continue
        progress = build_progress_summary(
            record.path,
            remote_running=session_id in app.resume_jobs,
        )
        state = str(progress.get("state") or "")
        if session_id not in app.resume_jobs and state in {"waiting", "aborted"}:
            app.remote_marks.pop(session_id, None)
            removed = True
            continue
        item = as_session_item(record)
        item["remote_mark_started_at"] = mark.started_at
        item["remote_mark_prompt"] = mark.prompt
        item["remote_mark_log_path"] = mark.log_path
        item["progress"] = progress
        item["remote_guard_active"] = True
        items.append(item)

    if removed:
        save_remote_marks(app.remote_marks_path, app.remote_marks)

    items.sort(
        key=lambda item: (
            str(item.get("remote_mark_started_at") or ""),
            str(item.get("timestamp") or ""),
        ),
        reverse=True,
    )
    return items


class SessionServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], context: AppContext):
        super().__init__(server_address, SessionHandler)
        self.app_context = context


class SessionHandler(BaseHTTPRequestHandler):
    server_version = "codex-sessions-web/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def app(self) -> AppContext:
        return self.server.app_context  # type: ignore[attr-defined]

    def _requested_target_id(self, parsed: Any, payload: Optional[Dict[str, Any]] = None) -> str:
        if isinstance(payload, dict):
            raw = str(payload.get("target") or "").strip()
            if raw:
                return normalize_target_id(raw)
        query = parse_qs(parsed.query, keep_blank_values=True)
        return normalize_target_id(query.get("target", [""])[0])

    def _resolve_target(self, target_id: str) -> Optional[MachineTarget]:
        normalized = normalize_target_id(target_id)
        return self.app.targets.get(normalized)

    def _target_from_request(self, parsed: Any, payload: Optional[Dict[str, Any]] = None) -> Optional[MachineTarget]:
        target_id = self._requested_target_id(parsed, payload)
        existing = self._resolve_target(target_id)

        query = parse_qs(parsed.query, keep_blank_values=True)
        auth_mode = normalize_target_auth_mode(
            str(
                self.headers.get("X-Target-SSH-Auth")
                or query.get("ssh_auth", [""])[0]
                or (payload or {}).get("ssh_auth")
                or (existing.auth_mode if existing is not None else "")
                or "key"
            )
        )
        ssh_password = str(self.headers.get("X-Target-Password") or (payload or {}).get("ssh_password") or "")
        if existing is not None:
            return MachineTarget(
                target_id=existing.target_id,
                label=existing.label,
                kind=existing.kind,
                ssh_host=existing.ssh_host,
                ssh_user=existing.ssh_user,
                ssh_port=existing.ssh_port,
                base_url=existing.base_url,
                auth_mode=auth_mode,
                ssh_password=ssh_password,
            )

        ssh_host = str(query.get("ssh_host", [""])[0] or (payload or {}).get("ssh_host") or "").strip()
        ssh_user = str(query.get("ssh_user", [""])[0] or (payload or {}).get("ssh_user") or "").strip()
        if not ssh_host or not ssh_user:
            return None
        try:
            ssh_port = int(query.get("ssh_port", [""])[0] or (payload or {}).get("ssh_port") or 22)
        except Exception:
            ssh_port = 22
        ssh_port = max(1, min(65535, ssh_port))
        base_url = str(query.get("base_url", [""])[0] or (payload or {}).get("base_url") or "http://127.0.0.1:8765").strip() or "http://127.0.0.1:8765"
        label = str(query.get("target_label", [""])[0] or (payload or {}).get("target_label") or target_id).strip() or target_id
        normalized_id = target_id if target_id != LOCAL_TARGET_ID else make_target_id(
            ssh_user=ssh_user,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
        )
        return MachineTarget(
            target_id=normalized_id,
            label=label,
            kind="ssh",
            ssh_host=ssh_host,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            base_url=base_url,
            auth_mode=auth_mode,
            ssh_password=ssh_password,
        )

    def _proxyable_for_request(self, parsed: Any, method: str) -> bool:
        path = str(parsed.path or "")
        if method == "GET":
            return path in PROXYABLE_GET_PATHS
        if method == "POST":
            return path in PROXYABLE_POST_PATHS
        return False

    def _run_remote_target_request(
        self,
        target: MachineTarget,
        *,
        method: str,
        forwarded_path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> tuple[int, Dict[str, Any]]:
        if target.kind == "local":
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Local target should not be proxied"}

        body_text = ""
        if payload:
            clean_payload = dict(payload)
            clean_payload.pop("target", None)
            body_text = json.dumps(clean_payload, ensure_ascii=False, separators=(",", ":"))

        remote_request = {
            "base_url": target.base_url,
            "method": method,
            "path": forwarded_path,
            "body_text": body_text,
        }
        script_text = f"REQUEST = {remote_request!r}\n{REMOTE_API_PROXY_SCRIPT}"

        return self._run_remote_python_payload(target, script_text)

    def _run_remote_python_payload(
        self,
        target: MachineTarget,
        script_text: str,
        *,
        timeout: int = 40,
    ) -> tuple[int, Dict[str, Any]]:
        if target.kind == "local":
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Local target should not be proxied"}

        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=5",
            "-p",
            str(int(target.ssh_port or 22)),
            f"{target.ssh_user}@{target.ssh_host}",
            "python3",
            "-",
        ]
        env: Optional[Dict[str, str]] = None
        askpass_path: Optional[str] = None
        auth_mode = normalize_target_auth_mode(target.auth_mode)
        if auth_mode == "password":
            if not target.ssh_password:
                return HTTPStatus.BAD_REQUEST, {
                    "ok": False,
                    "error": f"SSH password required for target: {target.label}",
                }
            command[1:1] = [
                "-o",
                "PreferredAuthentications=password,keyboard-interactive",
                "-o",
                "PubkeyAuthentication=no",
            ]
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    prefix="codex-target-askpass-",
                    delete=False,
                ) as handle:
                    handle.write("#!/bin/sh\nprintf '%s\\n' \"$CODEX_TARGET_SSH_PASSWORD\"\n")
                    askpass_path = handle.name
                os.chmod(askpass_path, 0o700)
                env = os.environ.copy()
                env["DISPLAY"] = env.get("DISPLAY") or "codex-target-auth"
                env["SSH_ASKPASS"] = askpass_path
                env["SSH_ASKPASS_REQUIRE"] = "force"
                env["CODEX_TARGET_SSH_PASSWORD"] = target.ssh_password
                command = ["setsid", *command]
            except Exception as exc:
                if askpass_path:
                    try:
                        os.unlink(askpass_path)
                    except OSError:
                        pass
                return HTTPStatus.BAD_GATEWAY, {
                    "ok": False,
                    "error": f"Failed to prepare password auth for {target.label}: {exc}",
                }
        try:
            completed = subprocess.run(
                command,
                input=script_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return HTTPStatus.GATEWAY_TIMEOUT, {
                "ok": False,
                "error": f"Remote target timed out: {target.label}",
            }
        except Exception as exc:
            return HTTPStatus.BAD_GATEWAY, {
                "ok": False,
                "error": f"Remote target failed: {exc}",
            }
        finally:
            if askpass_path:
                try:
                    os.unlink(askpass_path)
                except OSError:
                    pass

        if completed.returncode != 0:
            error_text = (completed.stderr or completed.stdout or "").strip()
            return HTTPStatus.BAD_GATEWAY, {
                "ok": False,
                "error": f"SSH to {target.label} failed: {error_text or f'exit {completed.returncode}'}",
            }

        raw_output = (completed.stdout or "").strip()
        if not raw_output:
            return HTTPStatus.BAD_GATEWAY, {
                "ok": False,
                "error": f"Remote target returned no response: {target.label}",
            }

        try:
            proxy_payload = json.loads(raw_output)
        except Exception:
            return HTTPStatus.BAD_GATEWAY, {
                "ok": False,
                "error": f"Remote target returned invalid proxy payload: {target.label}",
                "raw": compact_preview(raw_output, 300),
            }

        status = int(proxy_payload.get("status") or HTTPStatus.BAD_GATEWAY)
        body_text = str(proxy_payload.get("body") or "")
        try:
            body = json.loads(body_text)
        except Exception:
            body = {"ok": False, "error": f"Remote target returned invalid JSON body: {target.label}"}
        if isinstance(body, dict):
            body.setdefault("target", target.target_id)
            body.setdefault("target_label", target.label)
        return status, body

    def _handle_check_target(self, payload: Dict[str, Any]) -> None:
        try:
            target = build_target_from_payload(payload)
            bind_host, bind_port = parse_target_base_url(target.base_url)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        remote_request = {
            "service_name": str(payload.get("service_name") or DEFAULT_REMOTE_SERVICE_NAME).strip() or DEFAULT_REMOTE_SERVICE_NAME,
            "bind_host": bind_host,
            "bind_port": bind_port,
            "remote_base": str(payload.get("remote_base") or DEFAULT_REMOTE_INSTALL_BASE).strip() or DEFAULT_REMOTE_INSTALL_BASE,
        }
        script_text = f"REQUEST = {remote_request!r}\n{REMOTE_TARGET_CHECK_SCRIPT}"
        status, response_payload = self._run_remote_python_payload(target, script_text, timeout=20)
        if status == HTTPStatus.OK and isinstance(response_payload, dict) and response_payload.get("ok") is True:
            response_payload = enrich_target_check_result(response_payload, Path(__file__).resolve().parent)
        self._send_json(status, response_payload)

    def _handle_bootstrap_target(self, payload: Dict[str, Any]) -> None:
        try:
            target = build_target_from_payload(payload)
            bind_host, bind_port = parse_target_base_url(target.base_url)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        helper_path = Path(__file__).with_name("codex_sessions_bootstrap.py")
        if not helper_path.exists():
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"Missing bootstrap helper: {helper_path}"},
            )
            return
        remote_request = {
            "service_name": str(payload.get("service_name") or DEFAULT_REMOTE_SERVICE_NAME).strip() or DEFAULT_REMOTE_SERVICE_NAME,
            "bind_host": bind_host,
            "bind_port": bind_port,
            "remote_base": str(payload.get("remote_base") or DEFAULT_REMOTE_INSTALL_BASE).strip() or DEFAULT_REMOTE_INSTALL_BASE,
        }
        script_text = f"REQUEST = {remote_request!r}\n{REMOTE_TARGET_CHECK_SCRIPT}"
        check_status, check_payload = self._run_remote_python_payload(target, script_text, timeout=20)
        if check_status == HTTPStatus.OK and isinstance(check_payload, dict) and check_payload.get("ok") is True:
            check_payload = enrich_target_check_result(check_payload, Path(__file__).resolve().parent)
            if str(check_payload.get("recommendation") or "") in {"legacy_process_conflict", "port_conflict", "port_occupied_without_service"}:
                listener_hint = compact_preview(str(check_payload.get("listener_command") or ""), 240)
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "error": (
                            f"Bootstrap blocked for {target.label}: remote port {bind_port} is occupied"
                            + (f" by {listener_hint}" if listener_hint else "")
                        ),
                        "check": check_payload,
                    },
                )
                return
        command = [
            sys.executable,
            str(helper_path),
            "--host",
            target.ssh_host,
            "--user",
            target.ssh_user,
            "--ssh-port",
            str(int(target.ssh_port or 22)),
            "--label",
            target.label,
            "--bind-host",
            bind_host,
            "--bind-port",
            str(bind_port),
            "--remote-base",
            str(payload.get("remote_base") or DEFAULT_REMOTE_INSTALL_BASE),
            "--service-name",
            str(payload.get("service_name") or DEFAULT_REMOTE_SERVICE_NAME),
            "--skip-target-config",
        ]
        env = os.environ.copy()
        if target.auth_mode == "password" and target.ssh_password:
            env["CODEX_BOOTSTRAP_SSH_PASSWORD"] = target.ssh_password
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=240,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self._send_json(
                HTTPStatus.GATEWAY_TIMEOUT,
                {"ok": False, "error": f"Bootstrap timed out for {target.label}"},
            )
            return
        stdout_text = (completed.stdout or "").strip()
        stderr_text = (completed.stderr or "").strip()
        if completed.returncode != 0:
            print(
                f"[bootstrap] target={target.label} rc={completed.returncode} "
                f"stdout={compact_preview(stdout_text, 600)!r} stderr={compact_preview(stderr_text, 600)!r}",
                flush=True,
            )
            detail = compact_preview(stderr_text or stdout_text, 240)
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "error": f"Bootstrap failed for {target.label}: {detail or f'exit {completed.returncode}'}",
                    "stdout": compact_preview(stdout_text, 500),
                    "stderr": compact_preview(stderr_text, 500),
                },
            )
            return
        print(
            f"[bootstrap] target={target.label} ok stdout={compact_preview(stdout_text, 300)!r}",
            flush=True,
        )

        with self.app.lock:
            self.app.targets[target.target_id] = MachineTarget(
                target_id=target.target_id,
                label=target.label,
                kind="ssh",
                ssh_host=target.ssh_host,
                ssh_user=target.ssh_user,
                ssh_port=target.ssh_port,
                base_url=target.base_url,
                auth_mode=target.auth_mode,
            )
            save_machine_targets(self.app.targets_path, self.app.targets)
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "Remote codex_manager bootstrapped and saved",
                "target": target_to_public_dict(target),
                "stdout": compact_preview(stdout_text, 500),
                "stderr": compact_preview(stderr_text, 500),
            },
        )

    def _maybe_proxy_remote_request(
        self,
        *,
        parsed: Any,
        method: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self._proxyable_for_request(parsed, method):
            return False
        target_id = self._requested_target_id(parsed, payload)
        target = self._target_from_request(parsed, payload)
        if target is None and target_id == LOCAL_TARGET_ID:
            return False
        if target is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Unknown target: {target_id}"})
            return True
        if target.kind == "local":
            return False
        forwarded_path = build_forwarded_api_path(parsed)
        status, response_payload = self._run_remote_target_request(
            target,
            method=method,
            forwarded_path=forwarded_path,
            payload=payload,
        )
        self._send_json(status, response_payload)
        return True

    def _send_json(
        self,
        status: int,
        payload: Dict[str, Any],
        *,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def _send_html(
        self,
        content: str,
        *,
        status: int = HTTPStatus.OK,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(
        self,
        location: str,
        *,
        status: int = HTTPStatus.SEE_OTHER,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()

    def _read_form(self) -> Dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""
        parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON body")
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def _request_cookies(self) -> SimpleCookie[str]:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        return cookie

    def _request_is_secure(self) -> bool:
        forwarded_proto = str(self.headers.get("X-Forwarded-Proto") or "").strip().lower()
        if forwarded_proto == "https":
            return True
        cf_visitor = str(self.headers.get("CF-Visitor") or "").lower()
        return '"scheme":"https"' in cf_visitor

    def _request_is_direct_local(self) -> bool:
        host = str(self.client_address[0] if self.client_address else "").strip()
        if not host:
            return False
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host in {"localhost"}
        if not is_loopback:
            return False
        proxy_headers = ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP", "Forwarded")
        return not any(str(self.headers.get(header) or "").strip() for header in proxy_headers)

    def _build_cookie_header(self, value: str, *, max_age: int) -> str:
        cookie = SimpleCookie()
        auth = self.app.auth
        assert auth is not None
        cookie[auth.cookie_name] = value
        morsel = cookie[auth.cookie_name]
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Strict"
        morsel["max-age"] = str(max_age)
        if self._request_is_secure():
            morsel["secure"] = True
        return cookie.output(header="").strip()

    def _parse_auth_session(self) -> Optional[Dict[str, Any]]:
        auth = self.app.auth
        if auth is None:
            return {"csrf": "", "exp": 0}
        if self._request_is_direct_local():
            return {"csrf": "", "exp": 0, "local_bypass": True}
        morsel = self._request_cookies().get(auth.cookie_name)
        if morsel is None:
            return None
        return parse_session_cookie(auth.session_secret, morsel.value)

    def _is_authenticated(self) -> bool:
        return self.app.auth is None or self._parse_auth_session() is not None

    def _csrf_token(self) -> str:
        session = self._parse_auth_session()
        if not session:
            return ""
        return str(session.get("csrf") or "")

    def _auth_headers(self) -> Dict[str, str]:
        return {"Cache-Control": "no-store"}

    def _clear_cookie_header(self) -> Optional[str]:
        if self.app.auth is None:
            return None
        return self._build_cookie_header("", max_age=0)

    def _api_unauthorized(self) -> None:
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {
                "ok": False,
                "error": "Authentication required",
                "login_url": "/login",
            },
            extra_headers=self._auth_headers(),
        )

    def _require_api_auth(self) -> bool:
        if self.app.auth is None:
            return True
        if self._is_authenticated():
            return True
        self._api_unauthorized()
        return False

    def _require_page_auth(self) -> bool:
        if self.app.auth is None or self._is_authenticated():
            return True
        parsed = urlparse(self.path)
        next_path = sanitize_next_path(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
        self._redirect(f"/login?next={quote(next_path, safe='')}", extra_headers=self._auth_headers())
        return False

    def _require_csrf(self) -> bool:
        if self.app.auth is None:
            return True
        if self._request_is_direct_local():
            return True
        expected = self._csrf_token()
        actual = str(self.headers.get("X-CSRF-Token") or "").strip()
        if expected and actual and hmac.compare_digest(actual, expected):
            return True
        self._send_json(
            HTTPStatus.FORBIDDEN,
            {
                "ok": False,
                "error": "Invalid CSRF token",
            },
            extra_headers=self._auth_headers(),
        )
        return False

    def _login_state(self) -> tuple[str, Optional[AuthFailureState], float]:
        key = client_ip_from_headers(self)
        with self.app.lock:
            prune_auth_failures(self.app)
            state = self.app.auth_failures.get(key)
        now = time.time()
        return key, state, now

    def _record_login_failure(self, ip_key: str, now: float) -> tuple[int, int]:
        with self.app.lock:
            prune_auth_failures(self.app)
            state = self.app.auth_failures.get(ip_key)
            if state is None or now - state.last_attempt > 24 * 3600:
                state = AuthFailureState(count=0, last_attempt=now, blocked_until=0.0)
                self.app.auth_failures[ip_key] = state
            state.count += 1
            state.last_attempt = now
            cooldown = auth_cooldown_seconds(state.count)
            state.blocked_until = now + cooldown if cooldown > 0 else 0.0
            return state.count, cooldown

    def _clear_login_failures(self, ip_key: str) -> None:
        with self.app.lock:
            self.app.auth_failures.pop(ip_key, None)

    def _render_login_page(self, *, error: str = "", next_path: str = "/", retry_after: int = 0) -> str:
        escaped_error = html.escape(error)
        escaped_next = html.escape(next_path, quote=True)
        message_block = ""
        if escaped_error:
            message_block = f'<div class="notice error">{escaped_error}</div>'
        elif retry_after > 0:
            message_block = f'<div class="notice error">登录已临时封禁，请 {retry_after}s 后再试。</div>'
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>Codex Sessions Login</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --panel: #ffffff;
      --line: #d9e2f0;
      --text: #1f2937;
      --muted: #6b7280;
      --accent: #0f766e;
      --danger: #b91c1c;
      --danger-soft: #fef2f2;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 20px;
      background:
        radial-gradient(circle at top, rgba(15, 118, 110, 0.08), transparent 28%),
        linear-gradient(180deg, #eef5ff 0%, #f7f9fc 100%);
      color: var(--text);
      font-family: "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif;
    }}
    .card {{
      width: min(420px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 16px 40px rgba(15, 23, 42, 0.10);
      padding: 22px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    p {{ margin: 0 0 18px; color: var(--muted); line-height: 1.5; font-size: 14px; }}
    form {{ display: grid; gap: 12px; }}
    input, button {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      font-size: 15px;
    }}
    input {{ background: #fff; color: var(--text); }}
    button {{
      cursor: pointer;
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
      font-weight: 600;
    }}
    .notice {{
      margin-bottom: 14px;
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 14px;
      line-height: 1.45;
    }}
    .notice.error {{
      color: var(--danger);
      background: var(--danger-soft);
      border: 1px solid #fecaca;
    }}
    .hint {{ margin-top: 12px; color: var(--muted); font-size: 12px; line-height: 1.45; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Codex Sessions</h1>
    <p>输入本地密码后进入管理页。登录失败会触发逐级冷却，避免被暴力猜解。</p>
    {message_block}
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{escaped_next}" />
      <input name="password" type="password" autocomplete="current-password" placeholder="Password" autofocus required />
      <button type="submit">登录</button>
    </form>
    <div class="hint">建议只经由 Cloudflare Tunnel 暴露此页面，服务本身继续只监听 127.0.0.1。</div>
  </div>
</body>
</html>
"""

    def _handle_login_page(self) -> None:
        if self.app.auth is None:
            self._redirect("/", extra_headers=self._auth_headers())
            return
        if self._is_authenticated():
            query = parse_qs(urlparse(self.path).query)
            next_path = sanitize_next_path(query.get("next", ["/"])[0])
            self._redirect(next_path, extra_headers=self._auth_headers())
            return
        query = parse_qs(urlparse(self.path).query)
        next_path = sanitize_next_path(query.get("next", ["/"])[0])
        _, state, now = self._login_state()
        retry_after = 0
        if state and state.blocked_until > now:
            retry_after = int(max(1, round(state.blocked_until - now)))
        self._send_html(
            self._render_login_page(next_path=next_path, retry_after=retry_after),
            extra_headers=self._auth_headers(),
        )

    def _handle_login(self) -> None:
        if self.app.auth is None:
            self._redirect("/", extra_headers=self._auth_headers())
            return
        form = self._read_form()
        next_path = sanitize_next_path(form.get("next", "/"))
        password = str(form.get("password") or "")
        ip_key, state, now = self._login_state()
        if state and state.blocked_until > now:
            retry_after = int(max(1, round(state.blocked_until - now)))
            self._send_html(
                self._render_login_page(
                    error=f"登录已临时封禁，请 {retry_after}s 后再试。",
                    next_path=next_path,
                    retry_after=retry_after,
                ),
                status=HTTPStatus.TOO_MANY_REQUESTS,
                extra_headers={**self._auth_headers(), "Retry-After": str(retry_after)},
            )
            return

        auth = self.app.auth
        assert auth is not None
        if not password or not verify_password(password, auth.password_hash):
            time.sleep(0.35)
            _, cooldown = self._record_login_failure(ip_key, time.time())
            error = "密码错误"
            if cooldown > 0:
                error += f"，已触发 {cooldown}s 冷却"
            self._send_html(
                self._render_login_page(error=error, next_path=next_path, retry_after=cooldown),
                status=HTTPStatus.UNAUTHORIZED if cooldown == 0 else HTTPStatus.TOO_MANY_REQUESTS,
                extra_headers={**self._auth_headers(), **({"Retry-After": str(cooldown)} if cooldown > 0 else {})},
            )
            return

        self._clear_login_failures(ip_key)
        csrf_token = secrets.token_urlsafe(24)
        cookie_value = make_session_cookie(auth.session_secret, csrf_token=csrf_token, ttl_seconds=auth.session_ttl_seconds)
        self._redirect(
            next_path,
            extra_headers={
                **self._auth_headers(),
                "Set-Cookie": self._build_cookie_header(cookie_value, max_age=auth.session_ttl_seconds),
            },
        )

    def _handle_logout(self) -> None:
        headers = self._auth_headers()
        clear_cookie = self._clear_cookie_header()
        if clear_cookie:
            headers["Set-Cookie"] = clear_cookie
        self._send_json(HTTPStatus.OK, {"ok": True, "message": "Logged out"}, extra_headers=headers)

    def _handle_auth_session(self) -> None:
        if self.app.auth is None:
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "authenticated": True,
                    "auth_enabled": False,
                    "csrf_token": "",
                },
                extra_headers=self._auth_headers(),
            )
            return
        if not self._require_api_auth():
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "authenticated": True,
                "auth_enabled": True,
                "csrf_token": self._csrf_token(),
                "local_bypass": self._request_is_direct_local(),
            },
            extra_headers=self._auth_headers(),
        )

    def _handle_targets(self) -> None:
        with self.app.lock:
            items = [target_to_public_dict(target) for target in self.app.targets.values()]
        items.sort(key=lambda item: (item.get("kind") != "local", str(item.get("label") or "").lower(), str(item.get("id") or "")))
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "targets": items,
                "count": len(items),
            },
        )

    def _handle_save_target(self, payload: Dict[str, Any]) -> None:
        try:
            target = build_target_from_payload(payload)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        status, response_payload = self._run_remote_target_request(
            target,
            method="GET",
            forwarded_path="/api/auth/session",
        )
        if status != HTTPStatus.OK:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "error": response_payload.get("error") or f"Failed to connect target: {target.label}",
                },
            )
            return

        with self.app.lock:
            self.app.targets[target.target_id] = MachineTarget(
                target_id=target.target_id,
                label=target.label,
                kind="ssh",
                ssh_host=target.ssh_host,
                ssh_user=target.ssh_user,
                ssh_port=target.ssh_port,
                base_url=target.base_url,
                auth_mode=target.auth_mode,
            )
            save_machine_targets(self.app.targets_path, self.app.targets)
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "Target saved",
                "target": target_to_public_dict(target),
            },
        )

    def _handle_delete_target(self, payload: Dict[str, Any]) -> None:
        target_id = normalize_target_id(str(payload.get("target") or payload.get("id") or "").strip())
        if not target_id or target_id == LOCAL_TARGET_ID:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Local target cannot be deleted"})
            return
        with self.app.lock:
            existing = self.app.targets.get(target_id)
            if existing is None or existing.kind == "local":
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Unknown target: {target_id}"})
                return
            self.app.targets.pop(target_id, None)
            save_machine_targets(self.app.targets_path, self.app.targets)
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "Target deleted",
                "target_id": target_id,
            },
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self._handle_login_page()
            return
        if parsed.path == "/api/auth/session":
            self._handle_auth_session()
            return
        if parsed.path == "/":
            if not self._require_page_auth():
                return
            self._send_html(INDEX_HTML)
            return
        if parsed.path == "/remote":
            if not self._require_page_auth():
                return
            self._send_html(REMOTE_HTML)
            return

        if parsed.path.startswith("/api/") and not self._require_api_auth():
            return

        if parsed.path == "/api/targets":
            self._handle_targets()
            return

        if self._maybe_proxy_remote_request(parsed=parsed, method="GET"):
            return

        if parsed.path == "/api/sessions":
            query = parse_qs(parsed.query)
            include_archived = parse_bool(query.get("archived", ["1"])[0], True)
            text_query = query.get("q", [""])[0].strip()
            source_label = query.get("source_label", [""])[0].strip()
            limit = parse_int(query.get("limit", ["300"])[0], default=300, minimum=1, maximum=1000)
            with self.app.lock:
                sessions = load_records_for_view(
                    app=self.app,
                    include_archived=include_archived,
                    query=text_query,
                    source_label=source_label,
                    limit=limit,
                )
                sources = build_source_options(self.app, include_archived=include_archived, query=text_query)
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "sessions": sessions,
                    "count": len(sessions),
                    "source_options": sources,
                },
            )
            return

        if parsed.path == "/api/sources":
            query = parse_qs(parsed.query)
            include_archived = parse_bool(query.get("archived", ["1"])[0], True)
            text_query = query.get("q", [""])[0].strip()
            with self.app.lock:
                sources = build_source_options(self.app, include_archived=include_archived, query=text_query)
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "sources": sources,
                    "count": len(sources),
                },
            )
            return

        if parsed.path == "/api/stats":
            query = parse_qs(parsed.query)
            include_archived = parse_bool(query.get("archived", ["1"])[0], True)
            text_query = query.get("q", [""])[0].strip()
            source_label = query.get("source_label", [""])[0].strip()
            with self.app.lock:
                stats = build_stats(
                    self.app,
                    include_archived=include_archived,
                    query=text_query,
                    source_label=source_label,
                )
            self._send_json(HTTPStatus.OK, {"ok": True, "stats": stats})
            return

        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            session_key = query.get("session", [""])[0].strip()
            limit = parse_int(query.get("limit", ["300"])[0], default=300, minimum=1, maximum=2000)
            rounds = parse_int(query.get("rounds", ["0"])[0], default=0, minimum=0, maximum=20)
            if not session_key:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
                return
            with self.app.lock:
                records = load_records(
                    codex_home=self.app.codex_home,
                    include_archived=True,
                    slack_db=self.app.slack_db,
                    aliases_db=self.app.aliases_db,
                )
                try:
                    record = find_record(records, session_key)
                except ValueError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                if rounds > 0:
                    history, total = read_session_rounds(record.path, rounds=rounds)
                else:
                    history, total = read_session_history(record.path, limit=limit)
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "session": as_session_item(record),
                    "history": history,
                    "count": len(history),
                    "total": total,
                    "rounds": rounds,
                },
            )
            return

        if parsed.path == "/api/events":
            query = parse_qs(parsed.query)
            session_key = query.get("session", [""])[0].strip()
            limit = parse_int(query.get("limit", ["60"])[0], default=60, minimum=20, maximum=160)
            cursor_text = query.get("cursor", [""])[0].strip()
            if not session_key:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
                return
            with self.app.lock:
                records = load_records(
                    codex_home=self.app.codex_home,
                    include_archived=True,
                    slack_db=self.app.slack_db,
                    aliases_db=self.app.aliases_db,
                )
                try:
                    record = find_record(records, session_key)
                except ValueError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                prune_resume_jobs(self.app)
                if cursor_text:
                    cursor = parse_int(cursor_text, default=0, minimum=0, maximum=2**63 - 1)
                    events, next_cursor, reset = read_session_events_since(record.path, cursor=cursor, limit=limit)
                    mode = "delta"
                else:
                    events, next_cursor = read_recent_session_events(record.path, limit=limit)
                    reset = False
                    mode = "tail"
                progress = build_progress_summary(
                    record.path,
                    remote_running=record.session_id in self.app.resume_jobs,
                    byte_limit=256 * 1024,
                    max_lines=400,
                )
                updated_at = iso_from_epoch(record.path.stat().st_mtime)
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "session": as_session_item(record),
                    "events": events,
                    "count": len(events),
                    "cursor": next_cursor,
                    "mode": mode,
                    "reset": reset,
                    "updated_at": updated_at,
                    "progress": progress,
                },
            )
            return

        if parsed.path == "/api/remote_sessions":
            query = parse_qs(parsed.query)
            text_query = query.get("q", [""])[0].strip()
            limit = parse_int(query.get("limit", ["12"])[0], default=12, minimum=1, maximum=50)
            with self.app.lock:
                sessions = build_remote_items(self.app, query=text_query, limit=limit)
                watchlist_count = len(self.app.remote_watchlist)
                auto_continue_count = count_auto_continue_watches(self.app.remote_watchlist)
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "sessions": sessions,
                    "count": len(sessions),
                    "watchlist_count": watchlist_count,
                    "auto_continue_count": auto_continue_count,
                },
            )
            return

        if parsed.path == "/api/remote_guard":
            with self.app.lock:
                items = build_remote_guard_items(self.app)
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "sessions": items,
                    "count": len(items),
                    "warning": "这些会话最近是从网页触发继续的。在它们回到“等你继续”前，不要从 VS Code 再发新消息。",
                },
            )
            return

        if parsed.path == "/api/progress":
            query = parse_qs(parsed.query)
            session_key = query.get("session", [""])[0].strip()
            if not session_key:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
                return
            with self.app.lock:
                records = load_records(
                    codex_home=self.app.codex_home,
                    include_archived=True,
                    slack_db=self.app.slack_db,
                    aliases_db=self.app.aliases_db,
                )
                try:
                    record = find_record(records, session_key)
                except ValueError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                prune_resume_jobs(self.app)
                progress = build_progress_summary(
                    record.path,
                    remote_running=record.session_id in self.app.resume_jobs,
                )
                updated_at = iso_from_epoch(record.path.stat().st_mtime)
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "session": as_session_item(record),
                    "updated_at": updated_at,
                    "progress": progress,
                },
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self._handle_login()
            return
        if parsed.path == "/api/logout":
            if not self._require_api_auth() or not self._require_csrf():
                return
            self._handle_logout()
            return
        if not self._require_api_auth() or not self._require_csrf():
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/api/targets":
            self._handle_save_target(payload)
            return
        if parsed.path == "/api/targets/delete":
            self._handle_delete_target(payload)
            return
        if parsed.path == "/api/targets/check":
            self._handle_check_target(payload)
            return
        if parsed.path == "/api/targets/bootstrap":
            self._handle_bootstrap_target(payload)
            return

        if self._maybe_proxy_remote_request(parsed=parsed, method="POST", payload=payload):
            return

        if parsed.path in {"/api/set_title", "/api/rename"}:
            self._handle_set_title(payload)
            return
        if parsed.path in {"/api/clear_title", "/api/unname"}:
            self._handle_clear_title(payload)
            return
        if parsed.path in {"/api/set_workdir", "/api/set_cwd"}:
            self._handle_set_workdir(payload)
            return
        if parsed.path == "/api/set_source":
            self._handle_set_source(payload)
            return
        if parsed.path == "/api/archive":
            self._handle_archive(payload)
            return
        if parsed.path == "/api/delete":
            self._handle_delete(payload)
            return
        if parsed.path == "/api/batch_archive":
            self._handle_batch_archive(payload)
            return
        if parsed.path == "/api/batch_delete":
            self._handle_batch_delete(payload)
            return
        if parsed.path == "/api/resume_cmd":
            self._handle_resume_cmd(payload)
            return
        if parsed.path == "/api/continue":
            self._handle_continue(payload)
            return
        if parsed.path == "/api/remote_watchlist":
            self._handle_remote_watchlist(payload)
            return
        if parsed.path == "/api/stop":
            self._handle_stop(payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def _handle_set_title(self, payload: Dict[str, Any]) -> None:
        session_key = str(payload.get("session", "")).strip()
        title = str(payload.get("title") or payload.get("alias") or "").strip()
        if not session_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
            return
        if not title:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `title`"})
            return
        if len(title) > 140:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Title too long (max 140)"})
            return

        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            try:
                record = find_record(records, session_key)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return
            result = set_session_title(
                record,
                self.app.aliases_db,
                title,
                codex_home=self.app.codex_home,
            )
            previous = str(result.get("title") or result.get("override_title") or "").strip()
            sync_status = str(result.get("official_title_sync") or result.get("thread_name_sync") or "")
            sync_error = str(
                result.get("official_title_sync_error")
                or result.get("thread_name_sync_error")
                or ""
            ).strip()

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "Session title updated",
                "session_id": record.session_id,
                "previous": previous,
                "title": title,
                "official_title_sync": sync_status or "unknown",
                "official_title_sync_error": sync_error,
                "thread_name_sync": sync_status or "unknown",
                "thread_name_sync_error": sync_error,
            },
        )

    def _handle_clear_title(self, payload: Dict[str, Any]) -> None:
        session_key = str(payload.get("session", "")).strip()
        if not session_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
            return

        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            try:
                record = find_record(records, session_key)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return
            result = set_session_title(
                record,
                self.app.aliases_db,
                "",
                codex_home=self.app.codex_home,
            )
            previous = str(result.get("title") or result.get("override_title") or "").strip()
            sync_status = str(result.get("official_title_sync") or result.get("thread_name_sync") or "")
            sync_error = str(
                result.get("official_title_sync_error")
                or result.get("thread_name_sync_error")
                or ""
            ).strip()

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "Session title removed" if previous else "No custom title found",
                "session_id": record.session_id,
                "previous": previous,
                "official_title_sync": sync_status or "unknown",
                "official_title_sync_error": sync_error,
                "thread_name_sync": sync_status or "unknown",
                "thread_name_sync_error": sync_error,
            },
        )

    def _handle_set_workdir(self, payload: Dict[str, Any]) -> None:
        session_key = str(payload.get("session", "")).strip()
        cwd = str(payload.get("cwd", "")).strip()
        if not session_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
            return
        if not cwd:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `workdir`"})
            return
        if "\n" in cwd or "\r" in cwd:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "workdir cannot contain newlines"})
            return
        if len(cwd) > 1024:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "workdir too long (max 1024)"})
            return

        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            try:
                record = find_record(records, session_key)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return
            result = set_session_cwd(
                record=record,
                aliases_db=self.app.aliases_db,
                slack_db=self.app.slack_db,
                cwd=cwd,
            )
            previous = str(result.get("cwd") or result.get("override_cwd") or "").strip()
            updated_threads = result.get("slack_threads", [])

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "workdir updated",
                "session_id": record.session_id,
                "previous": previous,
                "previous_display": display_path(previous) if previous else "",
                "cwd": display_path(cwd),
                "cwd_raw": cwd,
                "cwd_display": display_path(cwd),
                "updated_slack_threads": updated_threads,
            },
        )

    def _handle_set_source(self, payload: Dict[str, Any]) -> None:
        session_key = str(payload.get("session", "")).strip()
        source = str(payload.get("source", "")).strip()
        if not session_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
            return
        if not source:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `source`"})
            return
        if "\n" in source or "\r" in source:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "source cannot contain newlines"})
            return
        if len(source) > 1024:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "source too long (max 1024)"})
            return

        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            try:
                record = find_record(records, session_key)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return
            result = set_session_source(
                record=record,
                source=source,
                codex_home=self.app.codex_home,
            )
            previous_session_source = str(result.get("source") or "").strip()
            previous_client_source = str(result.get("previous_client_source") or "").strip()
            effective_source = str(result.get("effective_source") or source).strip()
            thread_state_row_found = bool(result.get("thread_state_row_found"))
            thread_state_error = str(result.get("thread_state_error") or "").strip()
            session_index_appended = bool(result.get("session_index_appended"))

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "Session source updated",
                "session_id": record.session_id,
                "source": source,
                "effective_source": effective_source,
                "previous_session_source": previous_session_source,
                "previous_client_source": previous_client_source,
                "thread_state_row_found": thread_state_row_found,
                "thread_state_error": thread_state_error,
                "session_index_appended": session_index_appended,
            },
        )

    def _handle_archive(self, payload: Dict[str, Any]) -> None:
        session_key = str(payload.get("session", "")).strip()
        if not session_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
            return

        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            try:
                record = find_record(records, session_key)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return

            if record.archived:
                self._send_json(HTTPStatus.OK, {"ok": True, "message": "Already archived"})
                return

            dst_dir = self.app.codex_home / "archived_sessions"
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst_path = dst_dir / record.path.name
            if dst_path.exists():
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"ok": False, "error": f"Archive target exists: {display_path(dst_path)}"},
                )
                return

            shutil.move(str(record.path), str(dst_path))

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "Archived",
                "session_id": record.session_id,
            },
        )

    def _handle_delete(self, payload: Dict[str, Any]) -> None:
        session_key = str(payload.get("session", "")).strip()
        confirm = str(payload.get("confirm", "")).strip()
        if not session_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
            return
        if confirm != "DELETE":
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Set confirm=DELETE"})
            return

        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            try:
                record = find_record(records, session_key)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return

            record.path.unlink(missing_ok=False)
            clear_session_overrides(self.app.aliases_db, record.session_id)

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "Deleted",
                "session_id": record.session_id,
            },
        )

    def _handle_batch_archive(self, payload: Dict[str, Any]) -> None:
        session_keys = parse_session_keys(payload)
        if not session_keys:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing non-empty `session_ids`"})
            return

        results: list[Dict[str, str]] = []
        archived_count = 0
        skipped_archived = 0
        not_found = 0
        conflict = 0

        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            for session_key in session_keys:
                try:
                    record = find_record(records, session_key)
                except ValueError as exc:
                    not_found += 1
                    results.append({"session": session_key, "status": "not_found", "error": str(exc)})
                    continue

                if record.archived:
                    skipped_archived += 1
                    results.append({"session": record.session_id, "status": "already_archived", "error": ""})
                    continue

                dst_dir = self.app.codex_home / "archived_sessions"
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst_path = dst_dir / record.path.name
                if dst_path.exists():
                    conflict += 1
                    results.append(
                        {
                            "session": record.session_id,
                            "status": "conflict",
                            "error": f"Archive target exists: {display_path(dst_path)}",
                        }
                    )
                    continue

                shutil.move(str(record.path), str(dst_path))
                archived_count += 1
                results.append({"session": record.session_id, "status": "archived", "error": ""})

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "requested": len(session_keys),
                "archived": archived_count,
                "already_archived": skipped_archived,
                "not_found": not_found,
                "conflict": conflict,
                "results": results,
            },
        )

    def _handle_batch_delete(self, payload: Dict[str, Any]) -> None:
        session_keys = parse_session_keys(payload)
        confirm = str(payload.get("confirm", "")).strip()
        if not session_keys:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing non-empty `session_ids`"})
            return
        if confirm != "DELETE":
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Set confirm=DELETE"})
            return

        results: list[Dict[str, str]] = []
        deleted_count = 0
        not_found = 0
        file_missing = 0

        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            for session_key in session_keys:
                try:
                    record = find_record(records, session_key)
                except ValueError as exc:
                    not_found += 1
                    results.append({"session": session_key, "status": "not_found", "error": str(exc)})
                    continue
                try:
                    record.path.unlink(missing_ok=False)
                except FileNotFoundError:
                    file_missing += 1
                    results.append(
                        {"session": record.session_id, "status": "missing_file", "error": "session file not found"}
                    )
                    continue
                clear_session_overrides(self.app.aliases_db, record.session_id)
                deleted_count += 1
                results.append({"session": record.session_id, "status": "deleted", "error": ""})

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "requested": len(session_keys),
                "deleted": deleted_count,
                "not_found": not_found,
                "missing_file": file_missing,
                "results": results,
            },
        )

    def _handle_resume_cmd(self, payload: Dict[str, Any]) -> None:
        session_key = str(payload.get("session", "")).strip()
        if not session_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
            return
        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            try:
                record = find_record(records, session_key)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "session_id": record.session_id,
                "command": (
                    f"codex resume --all {record.session_id}"
                ),
            },
        )

    def _handle_continue(self, payload: Dict[str, Any]) -> None:
        session_key = str(payload.get("session", "")).strip()
        prompt = str(payload.get("prompt") or DEFAULT_CONTINUE_PROMPT).strip()
        if not session_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
            return
        if not prompt:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `prompt`"})
            return
        if len(prompt) > 4000:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Prompt too long (max 4000)"})
            return

        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            try:
                record = find_record(records, session_key)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return

            existing, cwd_path, log_path, exit_code, error = launch_resume_for_record(
                self.app,
                record,
                prompt=prompt,
                origin_label="web",
            )
            if error and existing is not None and cwd_path is None:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "error": error,
                        "session_id": record.session_id,
                        "started_at": existing.started_at,
                        "log_path": display_path(existing.log_path),
                        "log_path_raw": str(existing.log_path),
                        "log_path_display": display_path(existing.log_path),
                    },
                )
                return
            if error:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "error": error,
                        "session_id": record.session_id,
                        "exit_code": exit_code,
                        "log_path": display_path(log_path) if log_path else "",
                        "log_path_raw": str(log_path) if log_path else "",
                        "log_path_display": display_path(log_path) if log_path else "",
                    },
                )
                return
            if existing is None or cwd_path is None or log_path is None:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "error": "Resume command did not produce a running job",
                    },
                )
                return

        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "message": "Continue prompt sent",
                "session_id": record.session_id,
                "prompt": prompt,
                "prompt_display": display_continue_prompt(prompt),
                "pid": existing.process.pid,
                "cwd": display_path(cwd_path),
                "cwd_raw": str(cwd_path),
                "cwd_display": display_path(cwd_path),
                "log_path": display_path(log_path),
                "log_path_raw": str(log_path),
                "log_path_display": display_path(log_path),
            },
        )

    def _handle_remote_watchlist(self, payload: Dict[str, Any]) -> None:
        session_key = str(payload.get("session", "")).strip()
        if not session_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
            return
        watched_specified = "watched" in payload
        watched = parse_bool(str(payload.get("watched", "true")), default=True)
        auto_continue_specified = "auto_continue" in payload
        auto_continue = parse_bool(str(payload.get("auto_continue", "false")), default=False)
        continue_prompt = str(payload.get("continue_prompt") or "").strip()
        if continue_prompt and len(continue_prompt) > 4000:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Prompt too long (max 4000)"})
            return

        kick_auto_continue = False
        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=False,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            try:
                record = find_record(records, session_key)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return

            watch = self.app.remote_watchlist.get(record.session_id)
            added_at = str(watch.added_at or "").strip() if watch else ""
            auto_continue_prompt = str(watch.continue_prompt or AUTO_CONTINUE_PROMPT).strip() if watch else AUTO_CONTINUE_PROMPT
            last_resumed_turn_id = str(watch.last_resumed_turn_id or "").strip() if watch else ""
            last_resumed_at = str(watch.last_resumed_at or "").strip() if watch else ""
            current_watched = bool(watch)
            current_auto_continue = bool(watch.auto_continue) if watch else False
            target_auto_continue = auto_continue if auto_continue_specified else current_auto_continue
            target_watched = watched if watched_specified else current_watched
            if watched_specified and not watched and not auto_continue_specified:
                target_auto_continue = False
            if target_auto_continue:
                target_watched = True
            if continue_prompt:
                auto_continue_prompt = continue_prompt
            if target_watched:
                if not added_at:
                    added_at = iso_now()
                self.app.remote_watchlist[record.session_id] = RemoteWatch(
                    session_id=record.session_id,
                    added_at=added_at,
                    auto_continue=target_auto_continue,
                    continue_prompt=auto_continue_prompt,
                    last_resumed_turn_id=last_resumed_turn_id,
                    last_resumed_at=last_resumed_at,
                )
                kick_auto_continue = target_auto_continue and not current_auto_continue
            else:
                self.app.remote_watchlist.pop(record.session_id, None)
                added_at = ""
                target_auto_continue = False
            save_remote_watchlist(self.app.remote_watchlist_path, self.app.remote_watchlist)

        if kick_auto_continue:
            auto_continue_tick(self.app)

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "session_id": record.session_id,
                "watched": target_watched,
                "watched_at": added_at,
                "watchlist_count": len(self.app.remote_watchlist),
                "auto_continue": target_auto_continue,
                "auto_continue_count": count_auto_continue_watches(self.app.remote_watchlist),
                "message": (
                    "已进入持续推进"
                    if target_auto_continue
                    else ("已加入远程关注" if target_watched else "已取消远程关注")
                ),
            },
        )

    def _handle_stop(self, payload: Dict[str, Any]) -> None:
        session_key = str(payload.get("session", "")).strip()
        if not session_key:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing `session`"})
            return

        with self.app.lock:
            records = load_records(
                codex_home=self.app.codex_home,
                include_archived=True,
                slack_db=self.app.slack_db,
                aliases_db=self.app.aliases_db,
            )
            try:
                record = find_record(records, session_key)
            except ValueError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return

            stopped_at = iso_now()
            job, error, exit_code = stop_resume_job(self.app, record.session_id)
            if job is None:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "error": error or "No running remote-triggered resume for this session",
                        "session_id": record.session_id,
                    },
                )
                return
            if error:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "error": error,
                        "session_id": record.session_id,
                        "log_path": display_path(job.log_path),
                        "log_path_raw": str(job.log_path),
                        "log_path_display": display_path(job.log_path),
                    },
                )
                return

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "Remote-triggered resume stopped",
                "session_id": record.session_id,
                "started_at": job.started_at,
                "stopped_at": stopped_at,
                "exit_code": exit_code,
                "log_path": display_path(job.log_path),
                "log_path_raw": str(job.log_path),
                "log_path_display": display_path(job.log_path),
            },
        )

REMOTE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Codex 轻量监看</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --line: #dbe3ef;
      --accent: #0f766e;
      --accent-soft: #ecfdf5;
      --warn: #b45309;
      --warn-soft: #fff7ed;
      --danger: #b91c1c;
      --danger-soft: #fef2f2;
      --shadow: 0 12px 30px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(15, 118, 110, 0.08), transparent 28%),
        linear-gradient(180deg, #eef5ff 0%, #f7f9fc 100%);
      color: var(--text);
      overflow-x: hidden;
      -webkit-text-size-adjust: 100%;
      text-size-adjust: 100%;
    }
    .wrap {
      width: min(100%, 860px);
      max-width: 860px;
      margin: 0 auto;
      padding: 16px;
    }
    .hero {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 12px;
      min-width: 0;
    }
    .hero > * { min-width: 0; }
    .hero h1 {
      margin: 0;
      font-size: 22px;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }
    .hero p {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .hero-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
      min-width: 0;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
      padding: 14px;
      min-width: 0;
    }
    .guard-panel {
      margin-bottom: 12px;
      border: 1px solid #fcd34d;
      background: linear-gradient(180deg, #fffbea 0%, #fff7d6 100%);
    }
    .guard-title {
      margin: 0 0 6px;
      font-size: 15px;
      font-weight: 700;
      color: #92400e;
    }
    .guard-note {
      margin: 0;
      color: #92400e;
      font-size: 13px;
      line-height: 1.5;
    }
    .guard-list {
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }
    .guard-item {
      border: 1px solid #fcd34d;
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.72);
      padding: 10px 12px;
    }
    .guard-item strong {
      color: #78350f;
    }
    .guard-item .mono {
      color: #7c2d12;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-bottom: 10px;
    }
    .toolbar label {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      color: var(--muted);
      font-size: 14px;
    }
    input, select, button {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 16px;
      background: #fff;
      color: var(--text);
    }
    input { flex: 1 1 260px; min-width: 0; }
    .toolbar label input {
      flex: 0 0 auto;
      width: 16px;
      min-width: 16px;
      height: 16px;
      margin: 0;
      padding: 0;
    }
    button {
      cursor: pointer;
      font-weight: 600;
    }
    button.primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    button.soft {
      background: #f8fafc;
      color: var(--text);
    }
    button.watch-active {
      background: var(--accent-soft);
      color: var(--accent);
      border-color: #99f6e4;
    }
    button.danger {
      background: var(--danger);
      color: #fff;
      border-color: var(--danger);
    }
    .filter-strip {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }
    .filter-btn {
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 13px;
      background: #f8fafc;
      color: var(--muted);
    }
    .filter-btn.active {
      background: #111827;
      color: #fff;
      border-color: #111827;
    }
    .meta, .toast {
      color: var(--muted);
      font-size: 13px;
      min-height: 18px;
    }
    .toast { margin-top: 12px; }
    .cards {
      display: grid;
      gap: 12px;
      margin-top: 12px;
      min-width: 0;
    }
    .card {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(248,250,252,0.94) 100%);
      min-width: 0;
    }
    .card.watched {
      border-color: #99f6e4;
      background:
        linear-gradient(180deg, rgba(236,253,245,0.92) 0%, rgba(255,255,255,0.98) 16%, rgba(248,250,252,0.94) 100%);
      box-shadow: inset 0 0 0 1px rgba(15, 118, 110, 0.08);
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      min-width: 0;
    }
    .card-head > * { min-width: 0; }
    .title {
      margin: 0;
      font-size: 17px;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }
    .subhead {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      word-break: break-word;
      overflow-wrap: anywhere;
    }
    .chips {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 12px;
      border: 1px solid var(--line);
      color: var(--muted);
      background: #fff;
    }
    .chip.state-running {
      color: var(--warn);
      border-color: #fed7aa;
      background: var(--warn-soft);
    }
    .chip.state-waiting {
      color: var(--accent);
      border-color: #99f6e4;
      background: var(--accent-soft);
    }
    .chip.state-queued {
      color: #1d4ed8;
      border-color: #bfdbfe;
      background: #eff6ff;
    }
    .chip.state-aborted {
      color: var(--danger);
      border-color: #fecaca;
      background: var(--danger-soft);
    }
    .chip.attention-active {
      color: var(--warn);
      border-color: #fed7aa;
      background: var(--warn-soft);
    }
    .chip.attention-completed {
      color: #166534;
      border-color: #bbf7d0;
      background: #f0fdf4;
    }
    .chip.attention-needs_attention {
      color: var(--danger);
      border-color: #fecaca;
      background: var(--danger-soft);
    }
    .chip.attention-check,
    .chip.attention-unknown {
      color: #1d4ed8;
      border-color: #bfdbfe;
      background: #eff6ff;
    }
    .preview {
      margin-top: 12px;
      padding: 12px;
      border-radius: 14px;
      background: #f8fafc;
      border: 1px solid var(--line);
      font-size: 14px;
      line-height: 1.55;
      white-space: pre-wrap;
      word-break: break-word;
      overflow-wrap: anywhere;
      max-height: min(54vh, 30rem);
      overflow: auto;
      overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
      scrollbar-gutter: stable both-edges;
      position: relative;
    }
    .preview.is-collapsed {
      max-height: min(18vh, 8.75rem);
      overflow: hidden;
    }
    .preview.is-collapsed::after {
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      height: 40px;
      background: linear-gradient(180deg, rgba(248,250,252,0) 0%, rgba(248,250,252,0.96) 100%);
      pointer-events: none;
    }
    .preview.preview-markdown {
      white-space: normal;
    }
    .preview.preview-markdown p { margin: 0 0 8px; }
    .preview.preview-markdown p:last-child { margin-bottom: 0; }
    .preview.preview-markdown ul, .preview.preview-markdown ol { margin: 0 0 8px 20px; padding: 0; }
    .preview.preview-markdown li { margin: 2px 0; }
    .preview.preview-markdown blockquote {
      margin: 0 0 8px;
      padding-left: 12px;
      border-left: 3px solid #cbd5e1;
      color: #475569;
    }
    .preview.preview-markdown pre {
      margin: 0 0 8px;
      padding: 10px 12px;
      border-radius: 10px;
      background: #0f172a;
      color: #e2e8f0;
      overflow: auto;
      max-width: 100%;
      font-size: 12px;
      line-height: 1.5;
    }
    .preview.preview-markdown code {
      font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
      font-size: 12px;
      background: #e2e8f0;
      padding: 0.1em 0.35em;
      border-radius: 6px;
    }
    .preview.preview-markdown pre code {
      background: transparent;
      padding: 0;
      color: inherit;
    }
    .preview.preview-markdown h1, .preview.preview-markdown h2, .preview.preview-markdown h3,
    .preview.preview-markdown h4, .preview.preview-markdown h5, .preview.preview-markdown h6 {
      margin: 0 0 8px;
      line-height: 1.3;
    }
    .preview.preview-markdown hr {
      border: 0;
      border-top: 1px solid var(--line);
      margin: 10px 0;
    }
    .preview.preview-markdown a { color: #0f766e; text-decoration: underline; }
    .preview.preview-markdown table {
      display: block;
      max-width: 100%;
      overflow-x: auto;
      border-collapse: collapse;
    }
    .preview-label {
      margin-top: 12px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.02em;
    }
    .preview-hint {
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .preview-actions {
      margin-top: 8px;
      display: flex;
      justify-content: flex-end;
    }
    .preview-toggle {
      padding: 7px 10px;
      border-radius: 999px;
      font-size: 12px;
      line-height: 1;
      background: #f8fafc;
      color: var(--muted);
    }
    .detail {
      margin-top: 10px;
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .detail strong {
      color: var(--text);
      font-weight: 600;
    }
    .card-controls {
      margin-top: 12px;
      display: grid;
      gap: 8px;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .mono {
      font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .empty {
      padding: 20px 8px;
      text-align: center;
      color: var(--muted);
    }
    .history-panel {
      position: fixed;
      top: 18px;
      left: 50%;
      transform: translateX(-50%);
      width: min(760px, calc(100vw - 24px));
      max-height: calc(100vh - 36px);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px;
      background: rgba(255, 255, 255, 0.985);
      box-shadow: var(--shadow);
      display: none;
      z-index: 31;
      overflow: hidden;
      backdrop-filter: blur(14px);
    }
    .history-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.18);
      display: none;
      z-index: 30;
    }
    .history-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }
    .history-title {
      font-size: 14px;
      font-weight: 700;
      color: var(--text);
    }
    .history-body {
      margin-top: 10px;
      display: grid;
      gap: 8px;
      max-height: min(56vh, 560px);
      overflow: auto;
    }
    .history-actions {
      display: flex;
      justify-content: flex-end;
      margin-top: 12px;
    }
    .target-panel {
      width: min(720px, calc(100vw - 24px));
    }
    .target-form-grid {
      margin-top: 12px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .form-field {
      display: grid;
      gap: 6px;
      font-size: 13px;
      color: var(--muted);
    }
    .form-field.wide {
      grid-column: 1 / -1;
    }
    .field-help {
      font-size: 12px;
      line-height: 1.45;
      color: var(--muted);
    }
    .target-password-field[data-hidden="true"] {
      display: none;
    }
    .history-item {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 10px 12px;
      background: #fff;
    }
    .history-item.user {
      border-left: 4px solid #0ea5e9;
    }
    .history-item.assistant {
      border-left: 4px solid #16a34a;
    }
    .history-meta {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }
    .history-text {
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.55;
      font-size: 14px;
    }
    @media (max-width: 640px) {
      .wrap { padding: 12px; }
      .hero { flex-direction: column; }
      .hero-actions { justify-content: flex-start; }
      .hero-actions > * { width: 100%; }
      .card-head { flex-direction: column; }
      .chips { justify-content: flex-start; }
      button, input, select { width: 100%; }
      .toolbar label { width: 100%; }
      .history-panel {
        top: 10px;
        width: calc(100vw - 14px);
        max-height: calc(100vh - 20px);
        padding: 12px;
      }
      .history-body { max-height: calc(100vh - 210px); }
      .preview { max-height: min(58vh, 34rem); }
      .preview.is-collapsed { max-height: min(16vh, 6.75rem); }
      .target-form-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div>
        <h1>Codex 轻量监看</h1>
        <p>离开电脑时，用它快速判断任务还在推进、像是已完成，还是停在需要你介入的位置。这个页面只读最近活跃会话的尾部摘要，不扫整段历史。</p>
      </div>
      <div class="hero-actions">
        <select id="targetSelect" aria-label="目标机器"></select>
        <button id="addTargetBtn" type="button">添加机器</button>
        <button id="refreshBtn" class="primary" type="button">刷新</button>
        <button id="fullBtn" type="button">完整页</button>
        <button id="logoutBtn" type="button">退出</button>
      </div>
    </div>

    <div id="guardPanel" class="panel guard-panel" style="display:none;">
      <div class="guard-title">Remote Guard</div>
      <p id="guardNote" class="guard-note"></p>
      <div id="guardList" class="guard-list"></div>
    </div>

    <div id="historyBackdrop" class="history-backdrop"></div>
    <div id="historyPanel" class="history-panel">
      <div class="history-head">
        <div id="historyTitle" class="history-title">历史</div>
        <button id="historyCloseBtn" type="button">关闭</button>
      </div>
      <div id="historyMeta" class="meta">请选择一条会话查看。</div>
      <div id="historyBody" class="history-body"></div>
      <div class="history-actions">
        <button id="historyCloseBtnBottom" type="button">关闭</button>
      </div>
    </div>
    <div id="targetBackdrop" class="history-backdrop"></div>
    <div id="targetPanel" class="history-panel target-panel">
      <div class="history-head">
        <div id="targetPanelTitle" class="history-title">添加目标机器</div>
        <button id="targetCloseBtn" type="button">关闭</button>
      </div>
      <div id="targetMeta" class="meta">填写目标机 SSH 信息。密码只保存在当前浏览器会话，不写入持久配置。</div>
      <div class="target-form-grid">
        <label class="form-field">
          机器显示名
          <input id="targetLabelInput" type="text" placeholder="例如 example-host" />
        </label>
        <label class="form-field">
          SSH 主机
          <input id="targetHostInput" type="text" placeholder="例如 192.168.1.25" />
        </label>
        <label class="form-field">
          SSH 用户
          <input id="targetUserInput" type="text" placeholder="例如 ubuntu" />
        </label>
        <label class="form-field">
          SSH 端口
          <input id="targetPortInput" type="number" min="1" max="65535" placeholder="22" />
        </label>
        <label class="form-field">
          SSH 认证方式
          <select id="targetAuthMode">
            <option value="key">SSH Key / Agent</option>
            <option value="password">密码</option>
          </select>
        </label>
        <label id="targetPasswordField" class="form-field target-password-field" data-hidden="true">
          SSH 密码
          <input id="targetPasswordInput" type="password" autocomplete="current-password" placeholder="仅当前浏览器会话保存" />
          <span class="field-help">密码只存在当前浏览器会话，不写入本地持久配置。</span>
        </label>
        <label class="form-field wide">
          目标机本地 codex_manager 地址
          <input id="targetBaseUrlInput" type="text" placeholder="http://127.0.0.1:8765" />
          <span class="field-help">目标机需要运行 loopback 绑定的 codex_sessions_web 服务。</span>
        </label>
      </div>
      <div class="history-actions">
        <button id="targetCancelBtn" type="button">取消</button>
        <button id="targetDeleteBtn" type="button" class="danger">删除机器</button>
        <button id="targetCheckBtn" type="button">检查远端</button>
        <button id="targetBootstrapBtn" type="button">部署并保存</button>
        <button id="targetSaveBtn" type="button" class="primary">保存并测试</button>
      </div>
    </div>

    <div class="panel">
      <div class="toolbar">
        <input id="q" type="text" placeholder="搜索 显示标题 / id / 工作目录 / 模型" />
        <select id="limit">
          <option value="8">8</option>
          <option value="12" selected>12</option>
          <option value="20">20</option>
        </select>
        <label><input id="autoRefresh" type="checkbox" checked /> 自动刷新</label>
      </div>
      <div id="filters" class="filter-strip"></div>
      <div id="meta" class="meta">加载中...</div>
      <div id="cards" class="cards"></div>
      <div id="toast" class="toast"></div>
    </div>
  </div>

  <script>
    const qEl = document.getElementById("q");
    const limitEl = document.getElementById("limit");
    const autoRefreshEl = document.getElementById("autoRefresh");
    const targetSelectEl = document.getElementById("targetSelect");
    const addTargetBtnEl = document.getElementById("addTargetBtn");
    const filtersEl = document.getElementById("filters");
    const cardsEl = document.getElementById("cards");
    const metaEl = document.getElementById("meta");
    const toastEl = document.getElementById("toast");
    const guardPanelEl = document.getElementById("guardPanel");
    const guardNoteEl = document.getElementById("guardNote");
    const guardListEl = document.getElementById("guardList");
    const historyBackdropEl = document.getElementById("historyBackdrop");
    const historyPanelEl = document.getElementById("historyPanel");
    const historyTitleEl = document.getElementById("historyTitle");
    const historyMetaEl = document.getElementById("historyMeta");
    const historyBodyEl = document.getElementById("historyBody");
    const historyCloseBtnEl = document.getElementById("historyCloseBtn");
    const historyCloseBtnBottomEl = document.getElementById("historyCloseBtnBottom");
    const targetBackdropEl = document.getElementById("targetBackdrop");
    const targetPanelEl = document.getElementById("targetPanel");
    const targetPanelTitleEl = document.getElementById("targetPanelTitle");
    const targetMetaEl = document.getElementById("targetMeta");
    const targetLabelInputEl = document.getElementById("targetLabelInput");
    const targetHostInputEl = document.getElementById("targetHostInput");
    const targetUserInputEl = document.getElementById("targetUserInput");
    const targetPortInputEl = document.getElementById("targetPortInput");
    const targetAuthModeEl = document.getElementById("targetAuthMode");
    const targetPasswordFieldEl = document.getElementById("targetPasswordField");
    const targetPasswordInputEl = document.getElementById("targetPasswordInput");
    const targetBaseUrlInputEl = document.getElementById("targetBaseUrlInput");
    const targetCloseBtnEl = document.getElementById("targetCloseBtn");
    const targetCancelBtnEl = document.getElementById("targetCancelBtn");
    const targetDeleteBtnEl = document.getElementById("targetDeleteBtn");
    const targetCheckBtnEl = document.getElementById("targetCheckBtn");
    const targetBootstrapBtnEl = document.getElementById("targetBootstrapBtn");
    const targetSaveBtnEl = document.getElementById("targetSaveBtn");
    const DEFAULT_CONTINUE_LABEL = "继续自动推进";
    const CONTINUOUS_ENTER_LABEL = "进入持续推进";
    const CONTINUOUS_STOP_LABEL = "停止持续推进";
    const CUSTOM_CONTINUE_LABEL = "人工补一句";
    const DEFAULT_CONTINUE_PROMPT = "继续推进当前任务，直到拿到可验证结果。必要时主动读取代码、修改文件、运行命令或测试，并在完成后直接汇报结果；不要停在分析、计划或只汇报下一步。";
    const AUTO_CONTINUE_PROMPT = "请继续持续推进";
    const LOCAL_TARGET_ID = "local";
    const TARGET_STORAGE_KEY = "codex-target-id";
    const TARGET_SECRETS_SESSION_KEY = "codex-target-secrets";
    const REMOTE_FILTER_STORAGE_KEY = "codex-remote-filter";
    const REMOTE_FILTERS = [
      { key: "all", label: "全部" },
      { key: "watched", label: "只看关注" },
      { key: "active", label: "自动推进中" },
      { key: "needs_attention", label: "需人工介入" },
      { key: "completed", label: "像是已完成" },
    ];
    let csrfToken = "";
    let currentFilter = "all";
    let remoteItems = [];
    let remoteWatchlistCount = 0;
    let remoteAutoContinueCount = 0;
    let targetItems = [];
    let currentTargetId = LOCAL_TARGET_ID;
    let targetSecrets = {};
    let targetEditorOriginalId = "";
    const expandedPreviewKeys = new Set();

    function toast(text, isError = false) {
      toastEl.textContent = text;
      toastEl.style.color = isError ? "#b91c1c" : "#6b7280";
    }

    function shouldAttachTarget(path) {
      return !(path.startsWith("/api/auth/session") || path.startsWith("/api/logout") || path.startsWith("/api/targets"));
    }

    function builtinLocalTarget() {
      return { id: LOCAL_TARGET_ID, label: "本机", kind: "local", ssh_host: "", ssh_user: "", ssh_port: 22, base_url: "", auth_mode: "key" };
    }

    function loadStoredTargetSecrets() {
      try {
        const raw = JSON.parse(window.sessionStorage.getItem(TARGET_SECRETS_SESSION_KEY) || "{}");
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
        const normalized = {};
        for (const [key, value] of Object.entries(raw)) {
          if (!key || !value || typeof value !== "object") continue;
          const password = String(value.password || "");
          if (password) normalized[String(key)] = { password };
        }
        return normalized;
      } catch (error) {
        return {};
      }
    }

    function saveTargetSecrets() {
      try {
        window.sessionStorage.setItem(TARGET_SECRETS_SESSION_KEY, JSON.stringify(targetSecrets));
      } catch (error) {
      }
    }

    function targetPasswordFor(targetId) {
      const secret = targetSecrets[String(targetId || "").trim()] || null;
      return secret ? String(secret.password || "") : "";
    }

    function currentTarget() {
      return targetItems.find((item) => item.id === currentTargetId) || builtinLocalTarget();
    }

    function withTarget(path, targetOverride = null) {
      if (!shouldAttachTarget(path)) return path;
      const url = new URL(path, window.location.origin);
      const target = targetOverride || currentTarget();
      url.searchParams.set("target", target.id || LOCAL_TARGET_ID);
      if (target.id && target.id !== LOCAL_TARGET_ID) {
        url.searchParams.set("target_label", target.label || target.id);
        url.searchParams.set("ssh_host", target.ssh_host || "");
        url.searchParams.set("ssh_user", target.ssh_user || "");
        url.searchParams.set("ssh_port", String(target.ssh_port || 22));
        url.searchParams.set("base_url", target.base_url || "http://127.0.0.1:8765");
      }
      return `${url.pathname}${url.search}`;
    }

    function buildTargetHeaders(targetOverride = null, passwordOverride = "") {
      const headers = {};
      const target = targetOverride || currentTarget();
      if (!target || target.id === LOCAL_TARGET_ID) return headers;
      const authMode = target.auth_mode === "password" ? "password" : "key";
      headers["X-Target-SSH-Auth"] = authMode;
      if (authMode === "password") {
        const password = String(passwordOverride || targetPasswordFor(target.id) || "");
        if (password) headers["X-Target-Password"] = password;
      }
      return headers;
    }

    function targetFetch(path, options = {}, targetOverride = null, passwordOverride = "") {
      const headers = new Headers(options.headers || {});
      for (const [key, value] of Object.entries(buildTargetHeaders(targetOverride, passwordOverride))) {
        if (value) headers.set(key, value);
      }
      return fetch(withTarget(path, targetOverride), { credentials: "same-origin", ...options, headers });
    }

    function loadSavedTarget() {
      try {
        return String(window.localStorage.getItem(TARGET_STORAGE_KEY) || "").trim() || LOCAL_TARGET_ID;
      } catch (error) {
        return LOCAL_TARGET_ID;
      }
    }

    function saveTarget(value) {
      try {
        window.localStorage.setItem(TARGET_STORAGE_KEY, String(value || LOCAL_TARGET_ID));
      } catch (error) {
      }
    }

    function clearLegacyTargetProfiles() {
      try {
        window.localStorage.removeItem("codex-target-profiles");
      } catch (error) {
      }
    }

    function currentTargetLabel() {
      const found = targetItems.find((item) => item.id === currentTargetId);
      return found ? String(found.label || found.id || currentTargetId) : currentTargetId;
    }

    function previewStateKey(session) {
      return `${currentTargetId}::${String(session.id || "").trim()}`;
    }

    function syncExpandedPreviewKeys(items) {
      const alive = new Set((items || []).map((session) => previewStateKey(session)));
      for (const key of Array.from(expandedPreviewKeys)) {
        if (!alive.has(key)) expandedPreviewKeys.delete(key);
      }
    }

    function renderTargetOptions() {
      targetSelectEl.innerHTML = "";
      for (const item of targetItems) {
        const option = document.createElement("option");
        option.value = String(item.id || "").trim();
        option.textContent = String(item.label || item.id || "unknown");
        targetSelectEl.appendChild(option);
      }
      const validIds = new Set(targetItems.map((item) => String(item.id || "").trim()).filter(Boolean));
      if (!validIds.has(currentTargetId)) currentTargetId = LOCAL_TARGET_ID;
      targetSelectEl.value = currentTargetId;
      saveTarget(currentTargetId);
    }

    function normalizeTargetList(items) {
      if (!Array.isArray(items)) return [];
      return items
        .filter((item) => item && typeof item === "object")
        .map((item) => ({
          id: String(item.id || "").trim(),
          label: String(item.label || item.id || "").trim(),
          kind: String(item.kind || "ssh").trim() || "ssh",
          ssh_host: String(item.ssh_host || "").trim(),
          ssh_user: String(item.ssh_user || "").trim(),
          ssh_port: Number.parseInt(String(item.ssh_port || "22"), 10) || 22,
          base_url: String(item.base_url || "http://127.0.0.1:8765").trim() || "http://127.0.0.1:8765",
          auth_mode: String(item.auth_mode || "key").trim().toLowerCase() === "password" ? "password" : "key",
        }))
        .filter((item) => item.id);
    }

    async function loadTargets(preserveCurrent = true) {
      targetSecrets = loadStoredTargetSecrets();
      clearLegacyTargetProfiles();
      let serverItems = [];
      try {
        const data = await api("/api/targets");
        serverItems = normalizeTargetList(data.targets || []);
      } catch (error) {
        serverItems = [builtinLocalTarget()];
      }
      if (!serverItems.some((item) => item.id === LOCAL_TARGET_ID)) serverItems.unshift(builtinLocalTarget());
      targetItems = serverItems;
      const preferred = preserveCurrent ? currentTargetId : loadSavedTarget();
      currentTargetId = targetItems.some((item) => item.id === preferred) ? preferred : LOCAL_TARGET_ID;
      renderTargetOptions();
    }

    async function testTargetConnection(target, passwordOverride = "") {
      const response = await targetFetch("/api/sessions?limit=1", {}, target, passwordOverride);
      const data = await response.json().catch(() => ({ ok: false, error: "Invalid JSON response" }));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      return data;
    }

    function syncTargetPasswordField() {
      const hidden = targetAuthModeEl.value !== "password";
      targetPasswordFieldEl.dataset.hidden = hidden ? "true" : "false";
      targetPasswordInputEl.disabled = hidden;
      targetPasswordInputEl.required = !hidden;
    }

    function openTargetEditor(target = null) {
      const activeTarget = target && target.id ? target : null;
      targetEditorOriginalId = activeTarget ? String(activeTarget.id || "").trim() : "";
      targetPanelTitleEl.textContent = activeTarget ? `编辑目标机器: ${activeTarget.label || activeTarget.id}` : "添加目标机器";
      targetMetaEl.textContent = "填写目标机 SSH 信息。密码只保存在当前浏览器会话，不写入持久配置。";
      targetLabelInputEl.value = activeTarget ? String(activeTarget.label || "") : "";
      targetHostInputEl.value = activeTarget ? String(activeTarget.ssh_host || "") : "";
      targetUserInputEl.value = activeTarget ? String(activeTarget.ssh_user || "") : "";
      targetPortInputEl.value = activeTarget ? String(activeTarget.ssh_port || 22) : "22";
      targetAuthModeEl.value = activeTarget && activeTarget.auth_mode === "password" ? "password" : "key";
      targetPasswordInputEl.value = activeTarget ? targetPasswordFor(activeTarget.id) : "";
      targetBaseUrlInputEl.value = activeTarget ? String(activeTarget.base_url || "") : "http://127.0.0.1:8765";
      syncTargetPasswordField();
      targetDeleteBtnEl.style.display = activeTarget && activeTarget.id !== LOCAL_TARGET_ID ? "" : "none";
      targetBackdropEl.style.display = "block";
      targetPanelEl.style.display = "block";
      targetLabelInputEl.focus();
      targetLabelInputEl.select();
    }

    function closeTargetEditor() {
      targetEditorOriginalId = "";
      targetDeleteBtnEl.style.display = "none";
      targetBackdropEl.style.display = "none";
      targetPanelEl.style.display = "none";
      targetMetaEl.textContent = "填写目标机 SSH 信息。密码只保存在当前浏览器会话，不写入持久配置。";
    }

    function currentTargetNeedsPassword() {
      const target = currentTarget();
      return !!(target && target.id !== LOCAL_TARGET_ID && target.auth_mode === "password" && !targetPasswordFor(target.id));
    }

    function maybeRequireCurrentTargetPassword() {
      if (!currentTargetNeedsPassword()) return false;
      toast(`目标机器 ${currentTargetLabel()} 需要重新输入 SSH 密码`, true);
      openTargetEditor(currentTarget());
      return true;
    }

    function buildTargetDraftFromEditor() {
      const label = String(targetLabelInputEl.value || "").trim();
      const sshHost = String(targetHostInputEl.value || "").trim();
      const sshUser = String(targetUserInputEl.value || "").trim();
      const sshPort = Number.parseInt(String(targetPortInputEl.value || "22").trim() || "22", 10);
      const authMode = targetAuthModeEl.value === "password" ? "password" : "key";
      const password = String(targetPasswordInputEl.value || "");
      const baseUrl = String(targetBaseUrlInputEl.value || "").trim() || "http://127.0.0.1:8765";
      if (!label) throw new Error("机器显示名不能为空");
      if (!sshHost) throw new Error("SSH 主机不能为空");
      if (!sshUser) throw new Error("SSH 用户不能为空");
      if (!Number.isFinite(sshPort) || sshPort <= 0 || sshPort > 65535) throw new Error("SSH 端口不合法");
      if (authMode === "password" && !password) throw new Error("密码认证模式下必须填写 SSH 密码");
      return {
        target: {
          id: `${sshUser.toLowerCase()}@${sshHost.toLowerCase()}:${sshPort}`,
          label,
          kind: "ssh",
          ssh_host: sshHost,
          ssh_user: sshUser,
          ssh_port: sshPort,
          base_url: baseUrl,
          auth_mode: authMode,
        },
        password,
      };
    }

    function buildTargetPayload(target, password) {
      return {
        label: target.label,
        ssh_host: target.ssh_host,
        ssh_user: target.ssh_user,
        ssh_port: target.ssh_port,
        base_url: target.base_url,
        auth_mode: target.auth_mode,
        ssh_password: password || "",
      };
    }

    function describeTargetCheck(data) {
      const bits = [];
      if (data.recommendation === "legacy_process_conflict") {
        bits.push("远端端口被旧手工实例占用，需先清理旧版再接管");
      } else if (data.recommendation === "port_conflict") {
        bits.push("远端端口已被占用，新服务无法接管");
      } else if (data.recommendation === "port_occupied_without_service") {
        bits.push("远端端口已被其他进程占用");
      } else if (data.recommendation === "upgrade_required_for_ui") {
        bits.push("远端已安装，但接口能力不足以支撑当前 UI，需升级");
      } else if (data.recommendation === "update_recommended") {
        bits.push("远端 codex_manager 已就绪，但版本落后于当前机器");
      } else if (data.recommendation === "ready_unknown_version") {
        bits.push("远端 codex_manager 已就绪，但版本未知");
      } else if (data.api_ready) {
        bits.push("远端 codex_manager 已就绪");
      } else if (data.service_active) {
        bits.push("服务在跑，但 API 暂不可达");
      } else if (data.service_installed) {
        bits.push("远端已安装，但服务未运行");
      } else {
        bits.push("远端尚未安装 codex_manager");
      }
      bits.push(`sudo: ${data.sudo_ok ? "ok" : "缺失"}`);
      bits.push(`python3: ${data.python_ok ? "ok" : "缺失"}`);
      bits.push(`curl: ${data.curl_ok ? "ok" : "缺失"}`);
      bits.push(`tar: ${data.tar_ok ? "ok" : "缺失"}`);
      bits.push(`sessions: ${data.compat_sessions ? "ok" : "缺失"}`);
      bits.push(`remote_sessions: ${data.compat_remote_sessions ? "ok" : "缺失"}`);
      if (data.compat_session_id) bits.push(`events: ${data.compat_events ? "ok" : "缺失"}`);
      if (data.listener_pid) bits.push(`占用进程 PID: ${data.listener_pid}`);
      if (data.listener_command) bits.push(`占用命令: ${data.listener_command}`);
      if (data.remote_version_label) bits.push(`远端版本: ${data.remote_version_label}`);
      if (data.local_version_label) bits.push(`当前版本: ${data.local_version_label}`);
      if (data.api_error) bits.push(`API: ${data.api_error}`);
      return bits.join(" | ");
    }

    async function saveTargetEditor() {
      const { target, password } = buildTargetDraftFromEditor();
      targetMetaEl.textContent = `正在检查远端：${target.label || target.id}`;
      const check = await api("/api/targets/check", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify(buildTargetPayload(target, password))
      });
      targetMetaEl.textContent = describeTargetCheck(check);
      if (!["ready", "ready_unknown_version"].includes(String(check.recommendation || ""))) {
        throw new Error("远端尚未达到可直接接管状态，请先检查或部署更新");
      }
      await api("/api/targets", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify(buildTargetPayload(target, password))
      });
      if (targetEditorOriginalId && targetEditorOriginalId !== target.id) {
        delete targetSecrets[targetEditorOriginalId];
      }
      if (target.auth_mode === "password") {
        targetSecrets[target.id] = { password };
      } else {
        delete targetSecrets[target.id];
      }
      saveTargetSecrets();
      await loadTargets(false);
      currentTargetId = target.id;
      renderTargetOptions();
      closeTargetEditor();
      toast(`已保存机器：${target.label || target.id}`);
    }

    async function checkTargetEditor() {
      const { target, password } = buildTargetDraftFromEditor();
      targetMetaEl.textContent = `正在检查远端：${target.label || target.id}`;
      const data = await api("/api/targets/check", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify(buildTargetPayload(target, password))
      });
      targetMetaEl.textContent = describeTargetCheck(data);
      return data;
    }

    async function bootstrapTargetEditor() {
      const { target, password } = buildTargetDraftFromEditor();
      targetMetaEl.textContent = `正在部署远端：${target.label || target.id}`;
      const data = await api("/api/targets/bootstrap", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify(buildTargetPayload(target, password))
      });
      await api("/api/targets", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify(buildTargetPayload(target, password))
      });
      if (targetEditorOriginalId && targetEditorOriginalId !== target.id) {
        delete targetSecrets[targetEditorOriginalId];
      }
      if (target.auth_mode === "password") {
        targetSecrets[target.id] = { password };
      } else {
        delete targetSecrets[target.id];
      }
      saveTargetSecrets();
      await loadTargets(false);
      currentTargetId = target.id;
      renderTargetOptions();
      closeTargetEditor();
      toast(data.message || `已部署并保存：${target.label || target.id}`);
      return data;
    }

    async function deleteTargetEditor() {
      const targetId = String(targetEditorOriginalId || "").trim();
      if (!targetId || targetId === LOCAL_TARGET_ID) return;
      const target = targetItems.find((item) => item.id === targetId) || currentTarget();
      if (!confirm(`删除目标机器 ${target.label || targetId} ?`)) return;
      const data = await api("/api/targets/delete", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ target: targetId })
      });
      delete targetSecrets[targetId];
      saveTargetSecrets();
      await loadTargets(false);
      if (currentTargetId === targetId) {
        currentTargetId = LOCAL_TARGET_ID;
        saveTarget(currentTargetId);
      }
      closeTargetEditor();
      toast(data.message || `已删除机器：${target.label || targetId}`);
    }

    async function api(path, options = {}, targetOverride = null, passwordOverride = "") {
      const response = await targetFetch(path, options, targetOverride, passwordOverride);
      const data = await response.json().catch(() => ({ ok: false, error: "Invalid JSON response" }));
      if (response.status === 401 && data.login_url) {
        window.location.href = data.login_url;
        throw new Error("Authentication required");
      }
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      return data;
    }

    function jsonHeaders() {
      const headers = { "Content-Type": "application/json" };
      if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
      return headers;
    }

    async function bootstrapAuth() {
      const data = await api("/api/auth/session");
      csrfToken = data.csrf_token || "";
    }

    function shortId(value) {
      const text = String(value || "");
      if (text.length <= 18) return text;
      return `${text.slice(0, 8)}...${text.slice(-6)}`;
    }

    function relativeTime(epochMs) {
      const value = Number(epochMs || 0);
      if (!Number.isFinite(value) || value <= 0) return "-";
      const diffSeconds = Math.max(0, Math.round((Date.now() - value) / 1000));
      if (diffSeconds < 10) return "刚刚";
      if (diffSeconds < 60) return `${diffSeconds}s 前`;
      const diffMinutes = Math.round(diffSeconds / 60);
      if (diffMinutes < 60) return `${diffMinutes}m 前`;
      const diffHours = Math.round(diffMinutes / 60);
      if (diffHours < 48) return `${diffHours}h 前`;
      const diffDays = Math.round(diffHours / 24);
      return `${diffDays}d 前`;
    }

    function loadSavedFilter() {
      try {
        const value = String(window.localStorage.getItem(REMOTE_FILTER_STORAGE_KEY) || "").trim();
        if (REMOTE_FILTERS.some((item) => item.key === value)) return value;
      } catch (error) {
      }
      return "all";
    }

    function saveFilter(value) {
      try {
        window.localStorage.setItem(REMOTE_FILTER_STORAGE_KEY, value);
      } catch (error) {
      }
    }

    function filterLabel(value) {
      const found = REMOTE_FILTERS.find((item) => item.key === value);
      return found ? found.label : "全部";
    }

    function stateLabel(state) {
      if (state === "running") return "运行中";
      if (state === "waiting") return "已停下";
      if (state === "queued") return "已发出";
      if (state === "aborted") return "已中断";
      return "未知";
    }

    function attentionLabel(state) {
      if (state === "active") return "自动推进中";
      if (state === "completed") return "像是已完成";
      if (state === "needs_attention") return "需人工介入";
      if (state === "check") return "待确认";
      return "结果未明";
    }

    function continueHint(progress) {
      const runtime = String(progress.state || "");
      const attention = String(progress.attention_state || "");
      if (runtime === "running" || runtime === "queued") {
        return "当前看起来还在推进，先等结果；不要重复补发消息。";
      }
      if (attention === "completed") {
        if (progress.auto_continue_enabled) {
          return "看起来已经完成这一轮。因为已进入持续推进，后台会在下一次巡检时自动补一句。";
        }
        return "看起来已经完成这一轮；只有在你想追加目标时再发下一句。";
      }
      if (attention === "needs_attention") {
        return "它更像是停在需要判断或补充的位置；先人工补一句通常更稳。";
      }
      if (progress.auto_continue_enabled) {
        return "已进入持续推进。后台只会在明确 task_complete 停下后，再自动补一句。";
      }
      return "当前已停下；可以继续自动推进，或手动补一句纠偏。";
    }

    function progressPreviewLabel(progress) {
      const attention = String(progress.attention_state || "");
      if (attention === "completed") return "交付结果";
      if (attention === "needs_attention") return "介入线索";
      if (progress.last_assistant_text) return "本轮完整回复";
      return "最新摘要";
    }

    function matchesRemoteFilter(session, filterKey) {
      const progress = session.progress || {};
      if (filterKey === "watched") return Boolean(session.watched);
      if (filterKey === "active") return String(progress.attention_state || "") === "active";
      if (filterKey === "needs_attention") return String(progress.attention_state || "") === "needs_attention";
      if (filterKey === "completed") return String(progress.attention_state || "") === "completed";
      return true;
    }

    function filteredRemoteItems() {
      return remoteItems.filter((session) => matchesRemoteFilter(session, currentFilter));
    }

    function emptyStateText() {
      const hasQuery = Boolean(qEl.value.trim());
      if (currentFilter === "watched") {
        return hasQuery ? "当前搜索条件下没有已关注会话。" : "还没有远程关注。给需要盯结果的长任务点“加入关注”。";
      }
      if (currentFilter === "active") return "当前没有自动推进中的会话。";
      if (currentFilter === "needs_attention") return "当前没有明显需要人工介入的会话。";
      if (currentFilter === "completed") return "当前没有明显已经完成的会话。";
      return hasQuery ? "没有匹配到 session。" : "最近没有活跃会话。";
    }

    function renderFilters(items) {
      filtersEl.innerHTML = "";
      for (const config of REMOTE_FILTERS) {
        const count = items.filter((session) => matchesRemoteFilter(session, config.key)).length;
        const el = document.createElement("button");
        el.type = "button";
        el.className = `filter-btn ${config.key === currentFilter ? "active" : ""}`.trim();
        el.textContent = `${config.label} ${count}`;
        el.addEventListener("click", () => {
          currentFilter = config.key;
          saveFilter(currentFilter);
          renderRemote();
        });
        filtersEl.appendChild(el);
      }
    }

    function renderRemote() {
      renderFilters(remoteItems);
      const filtered = filteredRemoteItems();
      renderCards(filtered);
      metaEl.textContent =
        `机器: ${currentTargetLabel()} | 当前列表: ${remoteItems.length} | 远程关注: ${remoteWatchlistCount} | 持续推进: ${remoteAutoContinueCount} | 当前筛选: ${filterLabel(currentFilter)} ${filtered.length} | 自动刷新: ${autoRefreshEl.checked ? "开" : "关"}`;
    }

    function displayContinuePrompt(text) {
      const value = String(text || "").trim();
      if (!value) return DEFAULT_CONTINUE_LABEL;
      return value === DEFAULT_CONTINUE_PROMPT ? DEFAULT_CONTINUE_LABEL : value;
    }

    function escapeHtml(text) {
      return (text || "").replace(/[&<>\"']/g, (char) => {
        if (char === "&") return "&amp;";
        if (char === "<") return "&lt;";
        if (char === ">") return "&gt;";
        if (char === "\"") return "&quot;";
        return "&#39;";
      });
    }

    function sanitizeUrl(url) {
      const value = String(url || "").trim();
      if (!value) return "";
      if (/^(https?:\/\/|mailto:)/i.test(value)) {
        return value.replace(/"/g, "%22");
      }
      return "";
    }

    function renderInlineMarkdown(text) {
      let raw = String(text || "");
      const codeSpans = [];
      raw = raw.replace(/`([^`\n]+)`/g, (_, codeText) => {
        const idx = codeSpans.push(`<code>${escapeHtml(codeText)}</code>`) - 1;
        return `@@INLINE_CODE_${idx}@@`;
      });

      const links = [];
      raw = raw.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, href) => {
        const safeHref = sanitizeUrl(href);
        const safeLabel = escapeHtml(label);
        if (!safeHref) return safeLabel;
        const idx = links.push(`<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${safeLabel}</a>`) - 1;
        return `@@LINK_${idx}@@`;
      });

      let output = escapeHtml(raw);
      output = output.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
      output = output.replace(/(^|[^\*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
      output = output.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
      output = output.replace(/\n/g, "<br />");
      output = output.replace(/@@LINK_(\d+)@@/g, (_, idx) => links[Number(idx)] || "");
      output = output.replace(/@@INLINE_CODE_(\d+)@@/g, (_, idx) => codeSpans[Number(idx)] || "");
      return output;
    }

    function renderMarkdown(text) {
      let raw = String(text || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      const codeBlocks = [];
      raw = raw.replace(/```([^\n`]*)\n([\s\S]*?)```/g, (_, lang, codeText) => {
        const idx = codeBlocks.push({ lang: String(lang || "").trim(), code: codeText }) - 1;
        return `@@CODE_BLOCK_${idx}@@`;
      });

      const lines = raw.split("\n");
      const html = [];
      let index = 0;

      function isBlockStart(value) {
        const line = String(value || "").trim();
        if (!line) return false;
        if (/^@@CODE_BLOCK_\d+@@$/.test(line)) return true;
        if (/^#{1,6}\s+/.test(line)) return true;
        if (/^>\s?/.test(line)) return true;
        if (/^\s*[-*+]\s+/.test(line)) return true;
        if (/^\s*\d+\.\s+/.test(line)) return true;
        if (/^(\-{3,}|\*{3,}|_{3,})$/.test(line)) return true;
        return false;
      }

      while (index < lines.length) {
        const line = lines[index];
        const trimmed = String(line || "").trim();
        if (!trimmed) {
          index += 1;
          continue;
        }

        const codeMatch = trimmed.match(/^@@CODE_BLOCK_(\d+)@@$/);
        if (codeMatch) {
          const item = codeBlocks[Number(codeMatch[1])];
          if (item) {
            const className = item.lang ? ` class="language-${escapeHtml(item.lang)}"` : "";
            const codeHtml = escapeHtml(String(item.code || "").replace(/\n$/, ""));
            html.push(`<pre><code${className}>${codeHtml}</code></pre>`);
          }
          index += 1;
          continue;
        }

        const headingMatch = String(line).match(/^(#{1,6})\s+(.*)$/);
        if (headingMatch) {
          const level = headingMatch[1].length;
          html.push(`<h${level}>${renderInlineMarkdown(headingMatch[2].trim())}</h${level}>`);
          index += 1;
          continue;
        }

        if (/^>\s?/.test(trimmed)) {
          const quoteLines = [];
          while (index < lines.length && /^>\s?/.test(String(lines[index] || "").trim())) {
            quoteLines.push(String(lines[index] || "").replace(/^>\s?/, ""));
            index += 1;
          }
          html.push(`<blockquote>${renderInlineMarkdown(quoteLines.join("\n"))}</blockquote>`);
          continue;
        }

        if (/^\s*[-*+]\s+/.test(line)) {
          const items = [];
          while (index < lines.length && /^\s*[-*+]\s+/.test(String(lines[index] || ""))) {
            items.push(String(lines[index] || "").replace(/^\s*[-*+]\s+/, "").trim());
            index += 1;
          }
          html.push(`<ul>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
          continue;
        }

        if (/^\s*\d+\.\s+/.test(line)) {
          const items = [];
          while (index < lines.length && /^\s*\d+\.\s+/.test(String(lines[index] || ""))) {
            items.push(String(lines[index] || "").replace(/^\s*\d+\.\s+/, "").trim());
            index += 1;
          }
          html.push(`<ol>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ol>`);
          continue;
        }

        if (/^(\-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
          html.push("<hr />");
          index += 1;
          continue;
        }

        const paragraph = [line];
        index += 1;
        while (index < lines.length) {
          const nextLine = lines[index];
          if (!String(nextLine || "").trim()) break;
          if (isBlockStart(nextLine)) break;
          paragraph.push(nextLine);
          index += 1;
        }
        html.push(`<p>${renderInlineMarkdown(paragraph.join("\n").trim())}</p>`);
      }

      return html.join("\n");
    }

    function getSessionTitle(session) {
      return session.session_title || session.title || "";
    }

    function getOfficialTitle(session) {
      return session.official_title || session.thread_name || getSessionTitle(session) || "";
    }

    function getDisplayTitle(session) {
      return session.display_title || session.vscode_display_name || getOfficialTitle(session) || getSessionTitle(session) || session.id || "-";
    }

    function sessionDisplayName(session) {
      return String(session.alias || getDisplayTitle(session) || shortId(session.id)).trim() || shortId(session.id);
    }

    function buildDerivationDetails(session) {
      const parentId = String(session.parent_session_id || "").trim();
      if (!parentId) return [];
      const parts = [];
      parts.push(`派生自: ${session.parent_display_title || shortId(parentId)}`);
      const traits = [];
      if (session.subagent_role) traits.push(session.subagent_role);
      if (session.subagent_nickname) traits.push(session.subagent_nickname);
      if (Number(session.subagent_depth || 0) > 0) traits.push(`深度 ${session.subagent_depth}`);
      if (traits.length) parts.push(`子 Agent=${traits.join(" / ")}`);
      parts.push(`父会话ID=${shortId(parentId)}`);
      return parts;
    }

    function renderGuard(items, warning) {
      guardListEl.innerHTML = "";
      if (!items.length) {
        guardPanelEl.style.display = "none";
        return;
      }
      guardPanelEl.style.display = "block";
      guardNoteEl.textContent = warning || "这些会话最近由网页触发继续。回到“等你继续”前，不要从 VS Code 再发消息。";
      for (const session of items) {
        const progress = session.progress || {};
        const box = document.createElement("div");
        box.className = "guard-item";
        box.innerHTML =
          `<div><strong>${sessionDisplayName(session)}</strong></div>` +
          `<div class="mono">${shortId(session.id)} | ${stateLabel(progress.state)} | ${attentionLabel(progress.attention_state)} | ${session.remote_mark_started_at || "-"}</div>` +
          `<div>${progress.reason || "最近由网页触发继续"}</div>` +
          `<div>最近网页消息：${displayContinuePrompt(session.remote_mark_prompt)}</div>`;
        if (progress.remote_running) {
          const actions = document.createElement("div");
          actions.className = "actions";
          actions.appendChild(button("停止", "danger", () => stopContinue(session)));
          box.appendChild(actions);
        }
        guardListEl.appendChild(box);
      }
    }

    function chip(text, className = "") {
      const span = document.createElement("span");
      span.className = `chip ${className}`.trim();
      span.textContent = text;
      return span;
    }

    function closeHistory() {
      historyBackdropEl.style.display = "none";
      historyPanelEl.style.display = "none";
      historyTitleEl.textContent = "历史";
      historyMetaEl.textContent = "请选择一条会话查看。";
      historyBodyEl.innerHTML = "";
    }

    function renderHistory(session, historyItems, total) {
      historyBackdropEl.style.display = "block";
      historyPanelEl.style.display = "block";
      historyTitleEl.textContent = `最近 3 轮: ${sessionDisplayName(session)}`;
      historyMetaEl.textContent = `会话ID=${session.id} | 显示 ${historyItems.length} 条消息 / ${total} 轮`;
      historyBodyEl.innerHTML = "";
      for (const item of historyItems) {
        const box = document.createElement("div");
        box.className = `history-item ${item.role || "unknown"}`;

        const meta = document.createElement("div");
        meta.className = "history-meta";
        const phaseText = item.phase ? ` / ${item.phase}` : "";
        meta.textContent = `${item.timestamp || "-"} | ${item.role || "-"}${phaseText}`;
        box.appendChild(meta);

        const text = document.createElement("div");
        text.className = "history-text";
        text.textContent = item.text || "";
        box.appendChild(text);

        historyBodyEl.appendChild(box);
      }
      if (!historyItems.length) {
        const empty = document.createElement("div");
        empty.className = "history-meta";
        empty.textContent = "该会话暂无可展示的用户/助手消息。";
        historyBodyEl.appendChild(empty);
      }
    }

    function button(text, className, onClick) {
      const el = document.createElement("button");
      el.type = "button";
      el.textContent = text;
      if (className) el.className = className;
      el.addEventListener("click", onClick);
      return el;
    }

    function renderCards(items) {
      syncExpandedPreviewKeys(items);
      cardsEl.innerHTML = "";
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = emptyStateText();
        cardsEl.appendChild(empty);
        return;
      }

      for (const session of items) {
        const progress = session.progress || {};
        progress.auto_continue_enabled = Boolean(session.auto_continue);
        const card = document.createElement("div");
        card.className = "card";
        if (session.watched) card.classList.add("watched");

        const head = document.createElement("div");
        head.className = "card-head";

        const titleBox = document.createElement("div");
        const title = document.createElement("h2");
        title.className = "title";
        title.textContent = sessionDisplayName(session);
        titleBox.appendChild(title);

        const subhead = document.createElement("div");
        subhead.className = "subhead mono";
        subhead.textContent = `${shortId(session.id)} | ${session.model || "-"} | ${relativeTime(session.updated_at_epoch_ms)} | ${session.updated_at || "-"}`;
        titleBox.appendChild(subhead);
        head.appendChild(titleBox);

        const chips = document.createElement("div");
        chips.className = "chips";
        chips.appendChild(chip(stateLabel(progress.state), `state-${progress.state || "unknown"}`));
        chips.appendChild(chip(attentionLabel(progress.attention_state), `attention-${progress.attention_state || "unknown"}`));
        chips.appendChild(chip(session.source_short_label || session.source_label || "codex"));
        if (session.watched) chips.appendChild(chip("已关注", "attention-completed"));
        if (session.auto_continue) chips.appendChild(chip("持续推进", "state-running"));
        if (progress.remote_running) chips.appendChild(chip("网页发起"));
        head.appendChild(chips);
        card.appendChild(head);

        const controls = document.createElement("div");
        controls.className = "card-controls";
        const tools = document.createElement("div");
        tools.className = "actions";
        tools.appendChild(button(session.watched ? "取消关注" : "加入关注", session.watched ? "watch-active" : "soft", () => toggleWatch(session)));
        tools.appendChild(button(session.auto_continue ? CONTINUOUS_STOP_LABEL : CONTINUOUS_ENTER_LABEL, session.auto_continue ? "danger" : "primary", () => toggleAutoContinue(session)));
        if (progress.remote_running) tools.appendChild(button("停止续跑", "danger", () => stopContinue(session)));
        controls.appendChild(tools);

        if (!["running", "queued"].includes(String(progress.state || ""))) {
          const intervene = document.createElement("div");
          intervene.className = "actions";
          intervene.appendChild(button(DEFAULT_CONTINUE_LABEL, "primary", () => sendContinue(session, DEFAULT_CONTINUE_PROMPT)));
          intervene.appendChild(button(CUSTOM_CONTINUE_LABEL, "", () => customContinue(session)));
          controls.appendChild(intervene);
        }

        const controlsHint = document.createElement("div");
        controlsHint.className = "preview-hint";
        controlsHint.textContent = continueHint(progress);
        controls.appendChild(controlsHint);
        card.appendChild(controls);

        const previewHeading = document.createElement("div");
        previewHeading.className = "preview-label";
        previewHeading.textContent = progressPreviewLabel(progress);
        card.appendChild(previewHeading);

        const previewText = String(progress.last_assistant_text || progress.preview || progress.reason || "最近没有抓到可显示的尾部文本。");
        const preview = document.createElement("div");
        preview.className = "preview preview-markdown";
        preview.innerHTML = renderMarkdown(previewText);
        const previewKey = previewStateKey(session);
        const shouldCollapsePreview =
          String(progress.attention_state || "") === "completed" ||
          previewText.length > 280;
        const previewExpanded = expandedPreviewKeys.has(previewKey);
        if (shouldCollapsePreview && !previewExpanded) {
          preview.classList.add("is-collapsed");
        }
        card.appendChild(preview);
        if (shouldCollapsePreview) {
          const previewActions = document.createElement("div");
          previewActions.className = "preview-actions";
          const previewToggle = button(previewExpanded ? "收起内容" : "展开内容", "preview-toggle", () => {
            const collapsed = preview.classList.toggle("is-collapsed");
            if (collapsed) {
              expandedPreviewKeys.delete(previewKey);
            } else {
              expandedPreviewKeys.add(previewKey);
            }
            previewToggle.textContent = collapsed ? "展开内容" : "收起内容";
          });
          previewActions.appendChild(previewToggle);
          card.appendChild(previewActions);
        }

        const detail = document.createElement("div");
        detail.className = "detail";

        const reason = document.createElement("div");
        reason.innerHTML = `<strong>运行状态：</strong>${progress.reason || "-"}`;
        detail.appendChild(reason);

        const attention = document.createElement("div");
        attention.innerHTML = `<strong>结果信号：</strong>${progress.attention_reason || "-"}`;
        detail.appendChild(attention);

        const derivation = buildDerivationDetails(session);
        if (derivation.length) {
          const relation = document.createElement("div");
          relation.innerHTML = `<strong>派生关系：</strong>${derivation.join(" | ")}`;
          detail.appendChild(relation);
        }

        if (progress.last_user_preview) {
          const user = document.createElement("div");
          user.innerHTML = `<strong>最近用户：</strong>${displayContinuePrompt(progress.last_user_preview)}`;
          detail.appendChild(user);
        }

        if ((progress.assistant_segment_count || 0) > 1) {
          const count = document.createElement("div");
          count.innerHTML = `<strong>本轮回复片段：</strong>${progress.assistant_segment_count}`;
          detail.appendChild(count);
        }

        if (session.watched) {
          const watched = document.createElement("div");
          watched.innerHTML = `<strong>远程关注：</strong>${session.watched_at || "已置顶显示"}`;
          detail.appendChild(watched);
        }

        if (session.auto_continue) {
          const supervisor = document.createElement("div");
          const resumedAt = session.auto_continue_last_resumed_at ? ` | 最近续跑 ${session.auto_continue_last_resumed_at}` : "";
          supervisor.innerHTML = `<strong>持续推进：</strong>已开启（只在明确 task_complete 停下后再补一句）${resumedAt}`;
          detail.appendChild(supervisor);
        }

        if (progress.recent_tools && progress.recent_tools.length) {
          const tools = document.createElement("div");
          tools.innerHTML = `<strong>最近工具：</strong>${progress.recent_tools.join(", ")}`;
          detail.appendChild(tools);
        }

        const cwd = document.createElement("div");
        cwd.innerHTML = `<strong>工作目录：</strong><span class="mono">${session.cwd_display || session.cwd || "-"}</span>`;
        detail.appendChild(cwd);

        card.appendChild(detail);

        cardsEl.appendChild(card);
      }
    }

    async function loadRemote() {
      const query = encodeURIComponent(qEl.value.trim());
      const limit = encodeURIComponent(limitEl.value);
      const data = await api(`/api/remote_sessions?q=${query}&limit=${limit}`);
      remoteItems = data.sessions || [];
      remoteWatchlistCount = Number(data.watchlist_count || 0);
      remoteAutoContinueCount = Number(data.auto_continue_count || 0);
      renderRemote();
    }

    async function loadGuard() {
      const data = await api("/api/remote_guard");
      renderGuard(data.sessions || [], data.warning || "");
    }

    async function viewHistory(session) {
      try {
        toast(`加载最近 3 轮: ${session.id}`);
        const sessionKey = encodeURIComponent(session.id);
        const data = await api(`/api/history?session=${sessionKey}&rounds=3`);
        renderHistory(data.session, data.history || [], data.total || 0);
        toast(`历史已加载：${data.count} 条消息 / ${data.total} 轮`);
      } catch (error) {
        toast(error.message || String(error), true);
      }
    }

    async function refreshAll() {
      await bootstrapAuth();
      await loadTargets(true);
      if (maybeRequireCurrentTargetPassword()) return;
      await loadGuard();
      await loadRemote();
      toast("已刷新");
    }

    async function sendContinue(session, prompt) {
      try {
        const data = await api("/api/continue", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id, prompt })
        });
        toast(`已发出：${displayContinuePrompt(data.prompt_display || data.prompt)}。在 /remote 回到“等你继续”前，不要从 VS Code 再发消息。`);
        await refreshAll();
      } catch (error) {
        toast(error.message || String(error), true);
      }
    }

    async function toggleWatch(session) {
      try {
        const data = await api("/api/remote_watchlist", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id, watched: !session.watched })
        });
        toast(`${data.message}。当前远程关注 ${data.watchlist_count} 条。`);
        await loadRemote();
      } catch (error) {
        toast(error.message || String(error), true);
      }
    }

    async function toggleAutoContinue(session) {
      try {
        const data = await api("/api/remote_watchlist", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({
            session: session.id,
            watched: true,
            auto_continue: !session.auto_continue,
            continue_prompt: AUTO_CONTINUE_PROMPT
          })
        });
        toast(`${data.message}。当前持续推进 ${data.auto_continue_count} 条。`);
        await loadRemote();
      } catch (error) {
        toast(error.message || String(error), true);
      }
    }

    async function stopContinue(session) {
      if (!window.confirm(`停止该会话当前由网页触发的运行？\n${session.id}`)) return;
      try {
        const data = await api("/api/stop", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id })
        });
        toast(`已停止：${data.session_id}`);
        await refreshAll();
      } catch (error) {
        toast(error.message || String(error), true);
      }
    }

    async function customContinue(session) {
      const prompt = window.prompt("发给该会话的消息", "");
      if (prompt === null) return;
      const value = String(prompt || "").trim();
      if (!value) {
        toast("消息不能为空", true);
        return;
      }
      await sendContinue(session, value);
    }

    document.getElementById("refreshBtn").addEventListener("click", () => refreshAll().catch((e) => toast(e.message, true)));
    targetSelectEl.addEventListener("change", () => {
      currentTargetId = targetSelectEl.value || LOCAL_TARGET_ID;
      saveTarget(currentTargetId);
      if (maybeRequireCurrentTargetPassword()) return;
      refreshAll().catch((e) => toast(e.message, true));
    });
    addTargetBtnEl.addEventListener("click", () => {
      openTargetEditor(currentTargetId !== LOCAL_TARGET_ID ? currentTarget() : null);
    });
    document.getElementById("fullBtn").addEventListener("click", () => {
      window.location.href = "/";
    });
    document.getElementById("logoutBtn").addEventListener("click", async () => {
      try {
        await api("/api/logout", { method: "POST", headers: jsonHeaders() });
      } catch (error) {
        toast(error.message || String(error), true);
      } finally {
        window.location.href = "/login";
      }
    });
    historyCloseBtnEl.addEventListener("click", closeHistory);
    historyCloseBtnBottomEl.addEventListener("click", closeHistory);
    historyBackdropEl.addEventListener("click", closeHistory);
    targetCloseBtnEl.addEventListener("click", closeTargetEditor);
    targetCancelBtnEl.addEventListener("click", closeTargetEditor);
    targetDeleteBtnEl.addEventListener("click", () => deleteTargetEditor().then(() => refreshAll()).catch((e) => {
      targetMetaEl.textContent = e.message || String(e);
      toast(e.message || String(e), true);
    }));
    targetBackdropEl.addEventListener("click", closeTargetEditor);
    targetAuthModeEl.addEventListener("change", syncTargetPasswordField);
    targetCheckBtnEl.addEventListener("click", () => checkTargetEditor().catch((e) => {
      targetMetaEl.textContent = e.message || String(e);
      toast(e.message || String(e), true);
    }));
    targetBootstrapBtnEl.addEventListener("click", () => bootstrapTargetEditor().then(() => refreshAll()).catch((e) => {
      targetMetaEl.textContent = e.message || String(e);
      toast(e.message || String(e), true);
    }));
    targetSaveBtnEl.addEventListener("click", () => saveTargetEditor().then(() => refreshAll()).catch((e) => {
      targetMetaEl.textContent = e.message || String(e);
      toast(e.message || String(e), true);
    }));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && targetPanelEl.style.display === "block") {
        closeTargetEditor();
        return;
      }
      if (event.key === "Escape" && historyPanelEl.style.display === "block") closeHistory();
    });
    qEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadRemote().catch((e) => toast(e.message, true));
    });
    limitEl.addEventListener("change", () => loadRemote().catch((e) => toast(e.message, true)));
    currentTargetId = loadSavedTarget();
    currentFilter = loadSavedFilter();
    setInterval(() => {
      if (!autoRefreshEl.checked) return;
      refreshAll().catch((e) => toast(e.message, true));
    }, 10000);
    refreshAll().catch((e) => toast(e.message, true));
  </script>
</body>
</html>
"""


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Codex 会话管理器</title>
  <style>
    :root {
      --bg: #f7f9fc;
      --panel: #ffffff;
      --panel-soft: rgba(255, 255, 255, 0.78);
      --text: #1f2a37;
      --muted: #6b7280;
      --line: #e5e7eb;
      --accent: #0f766e;
      --accent-soft: #ecfdf5;
      --danger: #b91c1c;
      --danger-soft: #fef2f2;
      --warn: #b45309;
      --warn-soft: #fff7ed;
      --shadow: 0 20px 56px rgba(15, 23, 42, 0.1);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(15, 118, 110, 0.1), transparent 24%),
        radial-gradient(circle at top right, rgba(14, 165, 233, 0.08), transparent 28%),
        linear-gradient(180deg, #eef3fb 0%, #f8fafc 45%, #f7f9fc 100%);
      color: var(--text);
    }
    .wrap { max-width: 1840px; margin: 0 auto; padding: 20px; }
    .panel {
      background: linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(248,250,252,0.96) 100%);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }
    .intro-grid {
      display: grid;
      gap: 12px;
      margin-bottom: 12px;
    }
    .intro-main {
      display: grid;
      gap: 14px;
      min-width: 0;
    }
    .intro-aside {
      display: grid;
      gap: 10px;
      align-content: start;
      min-width: 0;
    }
    .page-hero {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 14px;
    }
    .eyebrow {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--accent);
    }
    .page-title {
      margin: 4px 0 0;
      font-size: 28px;
      line-height: 1.08;
      letter-spacing: -0.03em;
    }
    .page-subtitle {
      margin: 6px 0 0;
      max-width: 760px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }
    .hero-actions {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
    }
    .tips-panel {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(255,255,255,0.82);
      overflow: hidden;
    }
    .tips-summary {
      cursor: pointer;
      list-style: none;
      padding: 12px 14px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--muted);
      user-select: none;
    }
    .tips-summary::-webkit-details-marker {
      display: none;
    }
    .tips-summary::before {
      content: "▶";
      display: inline-block;
      margin-right: 8px;
      font-size: 11px;
      transition: transform 120ms ease;
    }
    .tips-panel[open] .tips-summary::before {
      transform: rotate(90deg);
    }
    .tips-body {
      padding: 0 14px 14px;
    }
    .guard-panel {
      margin-bottom: 10px;
      border: 1px solid #fcd34d;
      border-radius: 18px;
      background: linear-gradient(180deg, #fffbea 0%, #fff7d6 100%);
      overflow: hidden;
    }
    .guard-details {
      border: 0;
    }
    .guard-summary {
      cursor: pointer;
      list-style: none;
      padding: 12px 14px;
      font-size: 13px;
      font-weight: 700;
      color: #92400e;
      user-select: none;
    }
    .guard-summary::-webkit-details-marker {
      display: none;
    }
    .guard-summary::before {
      content: "▶";
      display: inline-block;
      margin-right: 8px;
      font-size: 11px;
      transition: transform 120ms ease;
    }
    .guard-details[open] .guard-summary::before {
      transform: rotate(90deg);
    }
    .guard-body {
      padding: 0 14px 14px;
    }
    .guard-title {
      margin: 0 0 6px;
      font-size: 14px;
      font-weight: 700;
      color: #92400e;
    }
    .guard-note {
      margin: 0;
      font-size: 13px;
      color: #92400e;
      line-height: 1.5;
    }
    .guard-list {
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }
    .guard-item {
      border: 1px solid #fcd34d;
      border-radius: 14px;
      background: rgba(255,255,255,0.72);
      padding: 10px 12px;
      font-size: 13px;
      line-height: 1.45;
    }
    .toolbar-shell {
      display: grid;
      gap: 10px;
    }
    .toolbar-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .toolbar-block {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: linear-gradient(180deg, rgba(255,255,255,0.92) 0%, rgba(245,248,252,0.94) 100%);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.85);
    }
    .toolbar-block.grow-block {
      flex: 1 1 720px;
    }
    .toolbar-block.action-block {
      flex: 1 1 320px;
    }
    .toolbar-label {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .grow { flex: 1 1 360px; min-width: 240px; }
    input, select, button {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 10px 12px;
      font-size: 14px;
      background: white;
      color: var(--text);
      min-height: 42px;
    }
    label.toggle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fff;
      min-height: 42px;
    }
    label.toggle input {
      margin: 0;
      width: 15px;
      height: 15px;
    }
    button {
      cursor: pointer;
      font-weight: 600;
      transition: transform 140ms ease, box-shadow 140ms ease, border-color 140ms ease;
    }
    button:hover {
      transform: translateY(-1px);
      box-shadow: 0 8px 18px rgba(15, 23, 42, 0.08);
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
      transform: none;
      box-shadow: none;
    }
    button.primary {
      background: var(--accent);
      color: white;
      border-color: var(--accent);
      box-shadow: 0 10px 22px rgba(15, 118, 110, 0.18);
    }
    button.warn {
      color: var(--warn);
      border-color: #fdba74;
      background: var(--warn-soft);
    }
    button.danger {
      color: var(--danger);
      border-color: #fca5a5;
      background: var(--danger-soft);
    }
    .meta, .toast { font-size: 13px; color: var(--muted); }
    .status-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .status-pill {
      display: flex;
      align-items: center;
      min-height: 40px;
      padding: 8px 12px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--panel-soft);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.88);
    }
    .status-pill.hint {
      color: var(--accent);
      border-color: #99f6e4;
      background: var(--accent-soft);
    }
    .tips-grid {
      display: grid;
      gap: 8px;
      grid-template-columns: 1fr;
    }
    .tip-card {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 10px 12px;
      background: rgba(255,255,255,0.82);
    }
    .tip-card-title {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      color: var(--text);
    }
    .tip-card-copy {
      margin-top: 6px;
      font-size: 12px;
      line-height: 1.55;
      color: var(--muted);
    }
    .session-layout {
      display: grid;
      gap: 14px;
    }
    .table-shell {
      max-height: 70vh;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 20px;
      background: rgba(255,255,255,0.72);
    }
    table {
      width: 100%;
      min-width: 1310px;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 13px;
      table-layout: fixed;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 12px 10px;
      vertical-align: top;
      text-align: left;
      background: transparent;
      overflow-wrap: anywhere;
    }
    tbody tr:hover td {
      background: rgba(15, 118, 110, 0.025);
    }
    th {
      background: rgba(248, 250, 252, 0.96);
      position: sticky;
      top: 0;
      z-index: 1;
      backdrop-filter: blur(10px);
    }
    .mono { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-size: 12px; }
    .cell-stack {
      display: grid;
      gap: 6px;
    }
    .cell-title {
      font-size: 13px;
      font-weight: 600;
      line-height: 1.45;
      color: var(--text);
      overflow-wrap: anywhere;
    }
    .cell-subtle {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    th:nth-child(3), td:nth-child(3) {
      min-width: 180px;
    }
    td:nth-child(3) .cell-title {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    td:nth-child(3) .mono {
      word-break: break-all;
    }
    th:nth-child(4), td:nth-child(4) {
      min-width: 224px;
    }
    th:nth-child(6), td:nth-child(6) {
      min-width: 118px;
    }
    th:nth-child(8), td:nth-child(8) {
      min-width: 82px;
    }
    th:nth-child(9), td:nth-child(9) {
      min-width: 150px;
    }
    th:nth-child(11), td:nth-child(11) {
      min-width: 162px;
    }
    .chip-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 12px;
      color: var(--muted);
      background: #fff;
    }
    .chip.accent {
      color: var(--accent);
      border-color: #99f6e4;
      background: var(--accent-soft);
    }
    .chip.warn {
      color: var(--warn);
      border-color: #fdba74;
      background: var(--warn-soft);
    }
    .chip.danger {
      color: var(--danger);
      border-color: #fca5a5;
      background: var(--danger-soft);
    }
    .row-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .compact-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }
    .compact-actions button {
      min-height: 34px;
      padding: 7px 10px;
      font-size: 12px;
      border-radius: 12px;
    }
    .action-stack {
      display: grid;
      gap: 10px;
      min-width: 0;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }
    .action-group {
      display: grid;
      gap: 8px;
      min-height: 100%;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.96) 0%, rgba(246,249,252,0.92) 100%);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.92);
    }
    .action-group-label {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .action-group-body {
      display: grid;
      gap: 6px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      align-content: start;
    }
    .action-group-body.single {
      grid-template-columns: 1fr;
    }
    .action-group-body button {
      width: 100%;
      min-height: 36px;
      padding: 8px 10px;
      font-size: 12px;
      border-radius: 12px;
    }
    .link-button {
      min-height: auto;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--accent);
      font-size: 12px;
      font-weight: 600;
      box-shadow: none;
    }
    .link-button:hover {
      transform: none;
      box-shadow: none;
      text-decoration: underline;
    }
    .inline-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px 8px;
      align-items: center;
    }
    .inline-note {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.4;
    }
    .truncate-line {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .session-expand-row td {
      padding-top: 0;
      background: rgba(248, 250, 252, 0.65);
    }
    .session-expand-row:hover td {
      background: rgba(248, 250, 252, 0.72);
    }
    .session-detail-panel {
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 12px;
      background: linear-gradient(180deg, rgba(255,255,255,0.94) 0%, rgba(248,250,252,0.92) 100%);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.92);
    }
    .session-detail-head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .session-detail-title {
      margin: 0;
      font-size: 14px;
      line-height: 1.25;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }
    .session-detail-subtitle {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .session-detail-layout {
      display: grid;
      gap: 10px;
      margin-top: 10px;
    }
    .session-utility-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .session-utility-row button {
      min-height: 34px;
      padding: 7px 10px;
      font-size: 12px;
      border-radius: 12px;
    }
    .session-context-grid {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    }
    .session-detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    .session-detail-actions {
      margin-top: 0;
    }
    .info-tile.rich .info-value {
      display: grid;
      gap: 6px;
      align-content: start;
    }
    .select-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }
    .select-pill input {
      margin: 0;
      width: 15px;
      height: 15px;
      padding: 0;
    }
    .title-main { font-weight: 500; }
    .title-sub { margin-top: 4px; font-size: 12px; color: var(--muted); }
    .toast { margin-top: 8px; font-size: 13px; color: var(--muted); min-height: 18px; }
    .session-cards {
      display: none;
      gap: 14px;
    }
    .session-card {
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 16px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(247,250,252,0.94) 100%);
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.08);
    }
    .session-card-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
    }
    .session-card-title {
      margin: 0;
      font-size: 18px;
      line-height: 1.2;
      letter-spacing: -0.02em;
    }
    .session-card-subtitle {
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
      word-break: break-word;
    }
    .session-card-chips {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
    }
    .session-card-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }
    .info-tile {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 10px 12px;
      background: rgba(255,255,255,0.82);
    }
    .info-label {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .info-value {
      margin-top: 5px;
      font-size: 13px;
      line-height: 1.5;
      color: var(--text);
      word-break: break-word;
    }
    .session-card-actions {
      margin-top: 14px;
      display: grid;
      gap: 8px;
    }
    .empty-state {
      padding: 18px;
      border: 1px dashed var(--line);
      border-radius: 18px;
      background: rgba(255,255,255,0.74);
      text-align: center;
      color: var(--muted);
    }
    .history-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.16);
      display: none;
      z-index: 20;
    }
    .history-panel {
      position: fixed;
      top: 18px;
      left: 50%;
      transform: translateX(-50%);
      width: min(920px, calc(100vw - 28px));
      max-height: calc(100vh - 36px);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: rgba(251, 253, 255, 0.985);
      display: none;
      box-shadow: 0 18px 44px rgba(15, 23, 42, 0.18);
      backdrop-filter: blur(14px);
      z-index: 21;
      overflow: hidden;
    }
    .history-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }
    .history-title { font-size: 13px; color: var(--text); }
    .history-body {
      margin-top: 8px;
      max-height: min(58vh, 620px);
      overflow: auto;
      display: grid;
      gap: 8px;
    }
    .history-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 12px;
    }
    .source-panel {
      width: min(720px, calc(100vw - 28px));
    }
    .source-editor {
      margin-top: 10px;
      display: grid;
      gap: 12px;
    }
    .source-help {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      background: rgba(255,255,255,0.82);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }
    .source-choice-grid {
      display: grid;
      gap: 10px;
    }
    .source-choice {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px 14px;
      background: rgba(255,255,255,0.88);
      text-align: left;
      color: var(--text);
    }
    .source-choice.active {
      border-color: rgba(15, 118, 110, 0.45);
      box-shadow: 0 0 0 3px rgba(15, 118, 110, 0.08);
      background: rgba(240, 253, 250, 0.96);
    }
    .source-choice-title {
      font-size: 14px;
      font-weight: 700;
      color: var(--text);
    }
    .source-choice-note {
      margin-top: 5px;
      font-size: 12px;
      line-height: 1.5;
      color: var(--muted);
    }
    .source-custom {
      display: grid;
      gap: 6px;
      font-size: 13px;
      color: var(--muted);
    }
    .source-custom input[disabled] {
      opacity: 0.55;
    }
    .source-preview {
      border: 1px dashed var(--line);
      border-radius: 14px;
      padding: 11px 13px;
      background: rgba(248,250,252,0.9);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
    }
    .target-panel {
      width: min(720px, calc(100vw - 24px));
    }
    .target-form-grid {
      margin-top: 12px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .form-field {
      display: grid;
      gap: 6px;
      font-size: 13px;
      color: var(--muted);
    }
    .form-field.wide {
      grid-column: 1 / -1;
    }
    .field-help {
      font-size: 12px;
      line-height: 1.45;
      color: var(--muted);
    }
    .target-password-field[data-hidden="true"] {
      display: none;
    }
    .history-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: #fff;
    }
    .history-item.user { border-left: 4px solid #0ea5e9; }
    .history-item.assistant { border-left: 4px solid #16a34a; }
    .history-meta { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
    .history-markdown { word-break: break-word; font-size: 13px; line-height: 1.45; }
    .history-markdown p { margin: 0 0 8px; }
    .history-markdown p:last-child { margin-bottom: 0; }
    .history-markdown ul, .history-markdown ol { margin: 0 0 8px 20px; padding: 0; }
    .history-markdown li { margin: 2px 0; }
    .history-markdown blockquote {
      margin: 0 0 8px;
      border-left: 3px solid #cbd5e1;
      color: #475569;
      padding-left: 10px;
    }
    .history-markdown pre {
      margin: 0 0 8px;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f8fafc;
      overflow: auto;
      font-size: 12px;
      line-height: 1.4;
    }
    .history-markdown code {
      font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
      background: #f1f5f9;
      border-radius: 4px;
      padding: 1px 4px;
    }
    .history-markdown pre code {
      background: transparent;
      border-radius: 0;
      padding: 0;
    }
    .history-markdown h1, .history-markdown h2, .history-markdown h3,
    .history-markdown h4, .history-markdown h5, .history-markdown h6 {
      margin: 0 0 8px;
      font-size: 14px;
      line-height: 1.35;
    }
    .history-markdown hr {
      border: 0;
      border-top: 1px solid var(--line);
      margin: 8px 0;
    }
    .history-markdown a { color: #0f766e; text-decoration: underline; }
    @media (min-width: 1480px) {
      .wrap {
        padding: 24px;
      }
      .panel {
        padding: 22px;
      }
      .intro-grid {
        grid-template-columns: minmax(0, 1.55fr) minmax(340px, 0.78fr);
        align-items: start;
      }
      .status-bar {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .page-subtitle {
        max-width: 1040px;
      }
    }
    @media (max-width: 1260px) {
      .intro-grid {
        grid-template-columns: 1fr;
      }
      .page-hero {
        flex-direction: column;
      }
      .hero-actions {
        justify-content: flex-start;
      }
      .table-shell {
        display: none;
      }
      .session-cards {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      }
    }
    @media (max-width: 920px) {
      .wrap { padding: 16px; }
      .panel { padding: 16px; }
      .tips-grid {
        grid-template-columns: 1fr;
      }
      .toolbar-row {
        flex-direction: column;
      }
      .toolbar-block {
        width: 100%;
      }
      .session-detail-layout,
      .session-detail-grid,
      .session-context-grid {
        grid-template-columns: 1fr;
      }
      .session-card-grid {
        grid-template-columns: 1fr;
      }
      .history-panel {
        top: 10px;
        width: calc(100vw - 16px);
        max-height: calc(100vh - 20px);
        padding: 12px;
      }
      .history-body { max-height: calc(100vh - 210px); }
      .target-form-grid {
        grid-template-columns: 1fr;
      }
    }
	    @media (max-width: 640px) {
	      .wrap { padding: 12px; }
	      .panel { padding: 14px; border-radius: 20px; }
	      .page-title { font-size: 25px; }
      .hero-actions {
        width: 100%;
        display: grid;
        grid-template-columns: 1fr;
      }
      input, select, button, label.toggle {
        width: 100%;
      }
      .status-bar {
        display: grid;
      }
      .session-cards {
        grid-template-columns: 1fr;
      }
      .session-card-head {
        flex-direction: column;
      }
	      .session-card-chips {
	        justify-content: flex-start;
	      }
	    }
	    .table-shell {
	      display: none;
	    }
	    .session-layout {
	      grid-template-columns: minmax(430px, 520px) minmax(0, 1fr);
	      align-items: start;
	      justify-content: start;
	    }
	    .session-cards {
	      display: grid;
	      gap: 8px;
	      max-height: calc(100vh - 80px);
	      overflow-y: auto;
	      overflow-x: hidden;
	      padding-right: 2px;
	      scrollbar-gutter: stable;
	    }
	    .fleet-card {
	      border: 1px solid var(--line);
	      border-radius: 16px;
	      padding: 12px;
	      background: linear-gradient(180deg, rgba(255,255,255,0.96) 0%, rgba(246,249,252,0.9) 100%);
	      box-shadow: inset 0 1px 0 rgba(255,255,255,0.9);
	      cursor: pointer;
	      transition: border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease;
	      min-width: 0;
	    }
	    .fleet-card:hover {
	      transform: translateY(-1px);
	      border-color: #8dd3c7;
	      box-shadow: 0 10px 22px rgba(15, 23, 42, 0.07);
	    }
	    .fleet-card.active {
	      border-color: #14b8a6;
	      box-shadow: 0 12px 24px rgba(20, 184, 166, 0.14);
	      background: linear-gradient(180deg, rgba(240,253,250,0.98) 0%, rgba(245,255,253,0.94) 100%);
	    }
	    .fleet-card-head {
	      display: flex;
	      justify-content: space-between;
	      gap: 10px;
	      align-items: flex-start;
	      min-width: 0;
	    }
	    .fleet-card-head > div {
	      min-width: 0;
	    }
	    .fleet-card-title {
	      margin: 0;
	      font-size: 14px;
	      line-height: 1.35;
	    }
	    .fleet-card-updated {
	      font-size: 12px;
	      color: var(--muted);
	      white-space: nowrap;
	    }
	    .fleet-card-subtitle {
	      margin-top: 4px;
	      color: var(--muted);
	      font-size: 12px;
	      line-height: 1.45;
	      word-break: break-word;
	    }
	    .fleet-card-preview {
	      margin-top: 8px;
	      font-size: 12px;
	      line-height: 1.5;
	      color: var(--text);
	      display: -webkit-box;
	      -webkit-line-clamp: 2;
	      -webkit-box-orient: vertical;
	      overflow: hidden;
	      word-break: break-word;
	    }
	    .fleet-card-footer {
	      margin-top: 8px;
	      display: flex;
	      gap: 8px;
	      align-items: center;
	      justify-content: space-between;
	      min-width: 0;
	    }
	    .fleet-card-tools {
	      font-size: 11px;
	      color: var(--muted);
	      line-height: 1.4;
	      flex: 1 1 auto;
	      min-width: 0;
	      white-space: nowrap;
	      overflow: hidden;
	      text-overflow: ellipsis;
	    }
	    .fleet-card-meta {
	      margin-top: 8px;
	      display: flex;
	      flex-wrap: wrap;
	      gap: 5px;
	      align-items: center;
	    }
	    .fleet-card-select {
	      display: inline-flex;
	      align-items: center;
	      gap: 6px;
	      font-size: 11px;
	      color: var(--muted);
	      padding-top: 0;
	      flex: 0 0 auto;
	      white-space: nowrap;
	    }
	    .fleet-card-select input {
	      margin: 0;
	    }
	    .live-shell {
	      border: 1px solid var(--line);
	      border-radius: 24px;
	      background: linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(245,248,252,0.96) 100%);
	      min-height: calc(100vh - 250px);
	      width: 100%;
	      max-width: 1040px;
	      display: grid;
	      grid-template-rows: auto auto 1fr auto;
	      overflow: hidden;
	      box-shadow: inset 0 1px 0 rgba(255,255,255,0.92);
	    }
	    .live-head {
	      padding: 18px 20px 12px;
	      border-bottom: 1px solid var(--line);
	      display: grid;
	      gap: 10px;
	      background: rgba(255,255,255,0.72);
	      backdrop-filter: blur(10px);
	    }
	    .live-title-row {
	      display: flex;
	      justify-content: space-between;
	      gap: 14px;
	      align-items: flex-start;
	    }
	    .live-title {
	      margin: 0;
	      font-size: 20px;
	      line-height: 1.3;
	    }
	    .live-subtitle {
	      color: var(--muted);
	      font-size: 13px;
	      line-height: 1.55;
	      word-break: break-word;
	    }
	    .live-status-row {
	      display: flex;
	      flex-wrap: wrap;
	      gap: 8px;
	      align-items: center;
	    }
	    .live-actions {
	      padding: 10px 20px;
	      border-bottom: 1px solid var(--line);
	      display: flex;
	      flex-wrap: wrap;
	      gap: 6px;
	      background: rgba(250,252,255,0.86);
	    }
	    .live-actions .status-pill {
	      min-height: 34px;
	      padding: 7px 10px;
	      font-size: 12px;
	      border-radius: 12px;
	    }
	    .live-actions button {
	      min-height: 36px;
	      padding: 8px 12px;
	      border-radius: 12px;
	      font-size: 12px;
	    }
	    .live-events {
	      min-height: 360px;
	      max-height: calc(100vh - 460px);
	      overflow: auto;
	      padding: 16px 20px 22px;
	      display: grid;
	      gap: 10px;
	      align-content: start;
	      scrollbar-gutter: stable both-edges;
	    }
	    .live-empty {
	      padding: 28px 20px;
	      color: var(--muted);
	      font-size: 13px;
	      line-height: 1.6;
	    }
	    .event-card {
	      border: 1px solid var(--line);
	      border-radius: 16px;
	      padding: 12px 14px;
	      background: #fff;
	      box-shadow: inset 0 1px 0 rgba(255,255,255,0.92);
	    }
	    .event-card.kind-commentary { border-left: 4px solid #0ea5e9; }
	    .event-card.kind-assistant_message { border-left: 4px solid #16a34a; }
	    .event-card.kind-user_message { border-left: 4px solid #6366f1; }
	    .event-card.kind-tool_call { border-left: 4px solid #d97706; }
	    .event-card.kind-tool_output { border-left: 4px solid #dc2626; }
	    .event-card.kind-reasoning { border-left: 4px solid #7c3aed; }
	    .event-card.kind-task_started,
	    .event-card.kind-task_complete,
	    .event-card.kind-turn_aborted,
	    .event-card.kind-token_count,
	    .event-card.kind-web_search { border-left: 4px solid #64748b; }
	    .event-head {
	      display: flex;
	      justify-content: space-between;
	      gap: 10px;
	      align-items: baseline;
	      flex-wrap: wrap;
	    }
	    .event-title {
	      font-size: 13px;
	      font-weight: 700;
	      color: var(--text);
	    }
	    .event-time {
	      font-size: 12px;
	      color: var(--muted);
	    }
	    .event-preview {
	      margin-top: 4px;
	      font-size: 12px;
	      color: var(--muted);
	      line-height: 1.45;
	      word-break: break-word;
	    }
	    .event-tags {
	      margin-top: 8px;
	      display: flex;
	      flex-wrap: wrap;
	      gap: 6px;
	    }
	    .event-body {
	      margin-top: 10px;
	      font-size: 13px;
	      line-height: 1.55;
	      word-break: break-word;
	    }
	    .event-details {
	      margin-top: 10px;
	      border: 1px dashed rgba(148,163,184,0.45);
	      border-radius: 14px;
	      background: rgba(248,250,252,0.78);
	      overflow: hidden;
	    }
	    .event-detail-summary {
	      cursor: pointer;
	      list-style: none;
	      padding: 10px 12px;
	      font-size: 12px;
	      color: var(--muted);
	      user-select: none;
	    }
	    .event-detail-summary::-webkit-details-marker {
	      display: none;
	    }
	    .event-detail-summary::before {
	      content: "▶";
	      display: inline-block;
	      margin-right: 8px;
	      font-size: 11px;
	      transition: transform 120ms ease;
	    }
	    .event-details[open] .event-detail-summary::before {
	      transform: rotate(90deg);
	    }
	    .event-details .event-body {
	      margin-top: 0;
	      padding: 0 12px 12px;
	    }
	    .event-body pre {
	      margin: 0;
	      padding: 10px 12px;
	      border-radius: 12px;
	      background: #0f172a;
	      color: #e2e8f0;
	      overflow: auto;
	      white-space: pre-wrap;
	      word-break: break-word;
	    }
	    .composer-shell {
	      border-top: 1px solid var(--line);
	      padding: 14px 20px 16px;
	      background: rgba(255,255,255,0.92);
	      display: grid;
	      gap: 8px;
	    }
	    .composer-top {
	      display: flex;
	      justify-content: space-between;
	      gap: 10px;
	      align-items: center;
	      flex-wrap: wrap;
	    }
	    .composer-title {
	      font-size: 12px;
	      font-weight: 700;
	      letter-spacing: 0.08em;
	      text-transform: uppercase;
	      color: var(--muted);
	    }
	    .composer-help {
	      font-size: 12px;
	      color: var(--muted);
	    }
	    .composer-input {
	      width: 100%;
	      min-height: 80px;
	      resize: vertical;
	      border: 1px solid var(--line);
	      border-radius: 16px;
	      padding: 12px 14px;
	      font-size: 14px;
	      line-height: 1.55;
	      font-family: inherit;
	      background: #fff;
	      color: var(--text);
	    }
	    .composer-actions {
	      display: flex;
	      flex-wrap: wrap;
	      gap: 8px;
	      align-items: center;
	    }
	    .composer-actions button {
	      min-height: 38px;
	      padding: 8px 12px;
	      border-radius: 12px;
	      font-size: 12px;
	    }
	    .live-note {
	      font-size: 12px;
	      color: var(--muted);
	      line-height: 1.5;
	    }
	    @media (max-width: 960px) {
	      .session-layout {
	        grid-template-columns: 1fr;
	      }
	      .session-cards {
	        max-height: none;
	      }
	      .live-shell {
	        min-height: 60vh;
	      }
	      .live-events {
	        max-height: none;
	      }
	    }
	  </style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <div class="intro-grid">
        <div class="intro-main">
          <div class="page-hero">
            <div>
              <div class="eyebrow">多机会话控制台</div>
              <h1 class="page-title">Codex 会话管理器</h1>
              <p class="page-subtitle">这个工具的核心不是“读 transcript”，而是整理会话身份、修标题、修可见客户端，以及从网页安全地继续推进任务。现在可以在页面上切换目标机器，沿用同一套管理动作。</p>
            </div>
            <div class="hero-actions">
              <select id="targetSelect" aria-label="目标机器"></select>
              <button id="addTargetBtn" type="button">添加机器</button>
              <button id="refresh" class="primary">刷新</button>
              <button id="remotePageBtn" type="button">轻量页</button>
              <button id="logoutBtn" type="button">退出</button>
            </div>
          </div>
          <div class="toolbar-shell">
            <div class="toolbar-block grow-block">
              <div class="toolbar-label">筛选</div>
              <input id="q" class="grow" placeholder="关键词：id/本地别名/显示标题/本地标题/工作目录/模型/大小..." />
              <select id="sourceFilter">
                <option value="">全部可见客户端</option>
              </select>
              <label class="toggle"><input id="archived" type="checkbox" checked /> 含归档</label>
              <select id="limit">
                <option value="100">100</option>
                <option value="300" selected>300</option>
                <option value="600">600</option>
              </select>
            </div>
            <div class="toolbar-row">
              <div class="toolbar-block action-block">
                <div class="toolbar-label">列表工具</div>
                <button id="statsBtn">统计</button>
                <button id="selectVisible">全选当前页</button>
                <button id="clearSelection">清空选择</button>
              </div>
              <div class="toolbar-block action-block">
                <div class="toolbar-label">批量动作</div>
                <button id="batchArchiveBtn" class="warn">批量归档已选</button>
                <button id="batchDeleteBtn" class="danger">批量删除已选</button>
              </div>
            </div>
          </div>
          <div class="status-bar">
            <div id="meta" class="meta status-pill">加载中...</div>
            <div id="selectionMeta" class="meta status-pill">已选 0 / 当前 0</div>
          </div>
        </div>
        <aside class="intro-aside">
          <details class="tips-panel">
            <summary class="tips-summary">使用提示（3）</summary>
            <div class="tips-body">
              <div class="tips-grid">
                <div class="tip-card">
                  <div class="tip-card-title">复制终端恢复命令</div>
                  <div class="tip-card-copy">只复制一条 <span class="mono">codex resume --all ...</span> 命令，会在终端里打开该会话，不会立刻替你自动续跑。</div>
                </div>
                <div class="tip-card">
                  <div class="tip-card-title">设可见客户端</div>
                  <div class="tip-card-copy">想让 VS Code 和本工具都能继续用，通常选“VS Code 扩展”。想让官方 Codex CLI picker 更容易发现它，选“Codex CLI”。</div>
                </div>
                <div class="tip-card">
                  <div class="tip-card-title">本工具 CLI 不看 source</div>
                  <div class="tip-card-copy">本工具的 <span class="mono">codex_sessions.py</span> 一直直接扫描磁盘上的 session；<span class="mono">source</span> 主要影响客户端列表里的可见性。</div>
                </div>
              </div>
            </div>
          </details>
        </aside>
      </div>
      <div id="guardPanel" class="guard-panel" style="display:none;">
        <details id="guardDetails" class="guard-details">
          <summary id="guardSummary" class="guard-summary">Remote Guard</summary>
          <div class="guard-body">
            <p id="guardNote" class="guard-note"></p>
            <div id="guardList" class="guard-list"></div>
          </div>
        </details>
      </div>
      <div id="historyBackdrop" class="history-backdrop"></div>
      <div class="session-layout">
        <div class="table-shell">
          <table>
            <colgroup>
              <col style="width: 36px;" />
              <col style="width: 108px;" />
              <col style="width: 180px;" />
              <col style="width: 224px;" />
              <col style="width: 78px;" />
              <col style="width: 118px;" />
              <col style="width: 96px;" />
              <col style="width: 82px;" />
              <col style="width: 150px;" />
              <col style="width: 60px;" />
              <col style="width: 162px;" />
            </colgroup>
            <thead>
              <tr>
                <th><input id="selectAll" type="checkbox" /></th>
                <th>最近变更</th>
                <th>id / 本地别名</th>
                <th>显示标题</th>
                <th>大小</th>
                <th>可见客户端</th>
                <th>模型</th>
                <th>状态</th>
                <th>工作目录</th>
                <th>Slack</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="rows"></tbody>
          </table>
	        </div>
	        <div id="sessionCards" class="session-cards"></div>
	        <section id="liveShell" class="live-shell">
	          <div class="live-head">
	            <div class="live-title-row">
	              <div>
	                <h2 id="liveSessionTitle" class="live-title">选择一个会话</h2>
	                <div id="liveSessionMeta" class="live-subtitle">默认页现在会持续观察一个滑动窗口，而不是一次性加载整份 transcript。</div>
	              </div>
	              <div id="liveSessionUpdated" class="fleet-card-updated">-</div>
	            </div>
	            <div id="liveStatusRow" class="live-status-row"></div>
	          </div>
	          <div id="liveActions" class="live-actions"></div>
	          <div id="liveEvents" class="live-events">
	            <div class="live-empty">左侧选择一个 session 后，这里会持续显示 commentary、tool call、tool output、token 变化和完成态，不再只看最终答复。</div>
	          </div>
	          <div class="composer-shell">
	            <div class="composer-top">
	              <div class="composer-title">插话 / 续跑</div>
	              <div id="liveComposerHelp" class="composer-help">对当前选中的 session 生效</div>
	            </div>
	            <textarea id="liveComposerInput" class="composer-input" placeholder="在这里给当前会话补一句话。留空时可以直接用“继续自动推进”。"></textarea>
	            <div class="composer-actions">
	              <button id="liveSendBtn" type="button" class="primary">发送这句话</button>
	              <button id="liveContinueBtn" type="button">继续自动推进</button>
	              <button id="liveStopBtn" type="button" class="danger">停止网页续跑</button>
	              <button id="liveRefreshBtn" type="button">刷新窗口</button>
	              <button id="liveHistoryBtn" type="button">最近 5 轮</button>
	              <button id="liveResumeCmdBtn" type="button">复制恢复命令</button>
	              <button id="liveSetTitleBtn" type="button">设标题</button>
	              <button id="liveSetSourceBtn" type="button">设可见客户端</button>
	              <button id="liveSetWorkdirBtn" type="button">设工作目录</button>
	            </div>
	            <div class="live-note">这不是完整历史浏览器，而是值班台：默认只看“现在”和“刚刚发生的事”。窗口只保留最近一小段 event timeline；需要时再向前翻最近 5 轮。</div>
	          </div>
	        </section>
	      </div>
      <div id="historyPanel" class="history-panel">
        <div class="history-head">
          <div id="historyTitle" class="history-title">历史</div>
          <button id="historyClose" type="button">关闭历史</button>
        </div>
        <div id="historyMeta" class="meta">请选择一条会话查看。</div>
        <div id="historyBody" class="history-body"></div>
        <div class="history-actions">
          <button id="historyCloseBottom" type="button">关闭历史</button>
        </div>
      </div>
      <div id="sourceBackdrop" class="history-backdrop"></div>
      <div id="sourcePanel" class="history-panel source-panel">
        <div class="history-head">
          <div id="sourcePanelTitle" class="history-title">设置可见客户端</div>
          <button id="sourceClose" type="button">关闭</button>
        </div>
        <div id="sourceMeta" class="meta">请选择一个目标客户端。</div>
        <div class="source-editor">
          <div class="source-help">同一个线程只能声明一个 source。想让 VS Code 和本工具都继续能用，通常选“VS Code 扩展”；想让官方 Codex CLI picker 优先发现它，选“Codex CLI”。本工具自己的 CLI 不依赖这个字段；如果你只是想从终端接管会话，直接用“复制终端恢复命令”或 <span class="mono">codex_sessions.py resume</span>。</div>
          <div id="sourceChoices" class="source-choice-grid"></div>
          <label class="source-custom">自定义 source
            <input id="sourceCustomInput" type="text" placeholder="例如 custom-lab" disabled />
          </label>
          <div id="sourcePreview" class="source-preview">请选择一个预设或输入自定义 source 值。</div>
        </div>
        <div class="history-actions">
          <button id="sourceCancel" type="button">取消</button>
          <button id="sourceSave" type="button" class="primary">保存</button>
        </div>
      </div>
      <div id="targetBackdrop" class="history-backdrop"></div>
      <div id="targetPanel" class="history-panel target-panel">
        <div class="history-head">
          <div id="targetPanelTitle" class="history-title">添加目标机器</div>
          <button id="targetClose" type="button">关闭</button>
        </div>
        <div id="targetMeta" class="meta">填写目标机 SSH 信息。密码只保存在当前浏览器会话，不写入持久配置。</div>
        <div class="target-form-grid">
          <label class="form-field">
            机器显示名
            <input id="targetLabelInput" type="text" placeholder="例如 example-host" />
          </label>
          <label class="form-field">
            SSH 主机
            <input id="targetHostInput" type="text" placeholder="例如 192.168.1.25" />
          </label>
          <label class="form-field">
            SSH 用户
            <input id="targetUserInput" type="text" placeholder="例如 ubuntu" />
          </label>
          <label class="form-field">
            SSH 端口
            <input id="targetPortInput" type="number" min="1" max="65535" placeholder="22" />
          </label>
          <label class="form-field">
            SSH 认证方式
            <select id="targetAuthMode">
              <option value="key">SSH Key / Agent</option>
              <option value="password">密码</option>
            </select>
          </label>
          <label id="targetPasswordField" class="form-field target-password-field" data-hidden="true">
            SSH 密码
            <input id="targetPasswordInput" type="password" autocomplete="current-password" placeholder="仅当前浏览器会话保存" />
            <span class="field-help">密码只存在当前浏览器会话，不写入本地持久配置。</span>
          </label>
          <label class="form-field wide">
            目标机本地 codex_manager 地址
            <input id="targetBaseUrlInput" type="text" placeholder="http://127.0.0.1:8765" />
            <span class="field-help">目标机需要运行 loopback 绑定的 codex_sessions_web 服务。</span>
          </label>
        </div>
      <div class="history-actions">
        <button id="targetCancel" type="button">取消</button>
        <button id="targetDelete" type="button" class="danger">删除机器</button>
        <button id="targetCheck" type="button">检查远端</button>
        <button id="targetBootstrap" type="button">部署并保存</button>
        <button id="targetSave" type="button" class="primary">保存并测试</button>
      </div>
      </div>
      <div id="toast" class="toast"></div>
    </div>
  </div>
  <script>
	    const state = {
	      sessions: [],
	      selected: new Set(),
	      sourceOptions: [],
	      remoteMarks: new Map(),
	      remoteGuardWarning: "",
	      guardOpen: false,
	      guardTouched: false,
	      sourceEditor: { session: null, choice: "", custom: "" },
	      expandedSessionId: "",
	      pendingFocusSessionId: "",
	      selectedSessionId: "",
	      liveSessionId: "",
	      liveEvents: [],
	      liveOpenDetailKeys: new Set(),
	      liveCursor: 0,
	      livePollInFlight: false,
	      livePollTimer: null,
	      liveStickToBottom: true,
	      liveForceScrollBottom: true,
	      sessionRefreshTimer: null,
	    };
    const qEl = document.getElementById("q");
    const targetSelectEl = document.getElementById("targetSelect");
    const addTargetBtnEl = document.getElementById("addTargetBtn");
    const sourceFilterEl = document.getElementById("sourceFilter");
    const archivedEl = document.getElementById("archived");
    const limitEl = document.getElementById("limit");
    const rowsEl = document.getElementById("rows");
    const cardsEl = document.getElementById("sessionCards");
    const metaEl = document.getElementById("meta");
    const toastEl = document.getElementById("toast");
    const selectionMetaEl = document.getElementById("selectionMeta");
    const selectAllEl = document.getElementById("selectAll");
    const batchArchiveBtnEl = document.getElementById("batchArchiveBtn");
    const batchDeleteBtnEl = document.getElementById("batchDeleteBtn");
    const historyBackdropEl = document.getElementById("historyBackdrop");
    const historyPanelEl = document.getElementById("historyPanel");
    const historyTitleEl = document.getElementById("historyTitle");
    const historyMetaEl = document.getElementById("historyMeta");
    const historyBodyEl = document.getElementById("historyBody");
    const historyCloseEl = document.getElementById("historyClose");
    const historyCloseBottomEl = document.getElementById("historyCloseBottom");
    const sourceBackdropEl = document.getElementById("sourceBackdrop");
    const sourcePanelEl = document.getElementById("sourcePanel");
    const sourcePanelTitleEl = document.getElementById("sourcePanelTitle");
    const sourceMetaEl = document.getElementById("sourceMeta");
    const sourceChoicesEl = document.getElementById("sourceChoices");
    const sourceCustomInputEl = document.getElementById("sourceCustomInput");
    const sourcePreviewEl = document.getElementById("sourcePreview");
    const sourceCloseEl = document.getElementById("sourceClose");
    const sourceCancelEl = document.getElementById("sourceCancel");
    const sourceSaveEl = document.getElementById("sourceSave");
    const targetBackdropEl = document.getElementById("targetBackdrop");
    const targetPanelEl = document.getElementById("targetPanel");
    const targetPanelTitleEl = document.getElementById("targetPanelTitle");
    const targetMetaEl = document.getElementById("targetMeta");
    const targetLabelInputEl = document.getElementById("targetLabelInput");
    const targetHostInputEl = document.getElementById("targetHostInput");
    const targetUserInputEl = document.getElementById("targetUserInput");
    const targetPortInputEl = document.getElementById("targetPortInput");
    const targetAuthModeEl = document.getElementById("targetAuthMode");
    const targetPasswordFieldEl = document.getElementById("targetPasswordField");
    const targetPasswordInputEl = document.getElementById("targetPasswordInput");
    const targetBaseUrlInputEl = document.getElementById("targetBaseUrlInput");
    const targetCloseEl = document.getElementById("targetClose");
    const targetCancelEl = document.getElementById("targetCancel");
    const targetDeleteEl = document.getElementById("targetDelete");
    const targetCheckEl = document.getElementById("targetCheck");
    const targetBootstrapEl = document.getElementById("targetBootstrap");
    const targetSaveEl = document.getElementById("targetSave");
	    const guardPanelEl = document.getElementById("guardPanel");
	    const guardDetailsEl = document.getElementById("guardDetails");
	    const guardSummaryEl = document.getElementById("guardSummary");
	    const guardNoteEl = document.getElementById("guardNote");
	    const guardListEl = document.getElementById("guardList");
	    const liveShellEl = document.getElementById("liveShell");
	    const liveSessionTitleEl = document.getElementById("liveSessionTitle");
	    const liveSessionMetaEl = document.getElementById("liveSessionMeta");
	    const liveSessionUpdatedEl = document.getElementById("liveSessionUpdated");
	    const liveStatusRowEl = document.getElementById("liveStatusRow");
	    const liveActionsEl = document.getElementById("liveActions");
	    const liveEventsEl = document.getElementById("liveEvents");
	    const liveComposerInputEl = document.getElementById("liveComposerInput");
	    const liveComposerHelpEl = document.getElementById("liveComposerHelp");
	    const liveSendBtnEl = document.getElementById("liveSendBtn");
	    const liveContinueBtnEl = document.getElementById("liveContinueBtn");
	    const liveStopBtnEl = document.getElementById("liveStopBtn");
	    const liveRefreshBtnEl = document.getElementById("liveRefreshBtn");
	    const liveHistoryBtnEl = document.getElementById("liveHistoryBtn");
	    const liveResumeCmdBtnEl = document.getElementById("liveResumeCmdBtn");
	    const liveSetTitleBtnEl = document.getElementById("liveSetTitleBtn");
	    const liveSetSourceBtnEl = document.getElementById("liveSetSourceBtn");
	    const liveSetWorkdirBtnEl = document.getElementById("liveSetWorkdirBtn");
	    const DEFAULT_CONTINUE_LABEL = "继续自动推进";
    const DEFAULT_CONTINUE_PROMPT = "继续推进当前任务，直到拿到可验证结果。必要时主动读取代码、修改文件、运行命令或测试，并在完成后直接汇报结果；不要停在分析、计划或只汇报下一步。";
    const LIVE_RESET_LIMIT = 60;
    const LIVE_DELTA_LIMIT = 80;
    const LIVE_EVENT_WINDOW = 120;
    const LOCAL_TARGET_ID = "local";
    const TARGET_STORAGE_KEY = "codex-target-id";
    const TARGET_SECRETS_SESSION_KEY = "codex-target-secrets";
    const SOURCE_PRESETS = [
      {
        value: "vscode",
        label: "VS Code 扩展",
        note: "推荐。优先让线程出现在 VS Code 会话列表，也适合和本工具一起继续使用。",
      },
      {
        value: "cli",
        label: "Codex CLI",
        note: "优先给官方 Codex CLI 的 resume picker，不影响本工具自己的 CLI 扫描。",
      },
      {
        value: "exec",
        label: "Exec / 非交互",
        note: "保持 exec 风格线程，适合脚本或一次性任务。",
      },
      {
        value: "__custom__",
        label: "自定义 source",
        note: "只有你明确知道目标客户端如何识别时才建议用。",
      },
    ];
    let csrfToken = "";
    let targetItems = [];
    let currentTargetId = LOCAL_TARGET_ID;
    let targetSecrets = {};
    let targetEditorOriginalId = "";

    function toast(text, isError = false) {
      toastEl.textContent = text;
      toastEl.style.color = isError ? "#b91c1c" : "#6b7280";
    }

    function displayContinuePrompt(text) {
      const value = String(text || "").trim();
      if (!value) return DEFAULT_CONTINUE_LABEL;
      return value === DEFAULT_CONTINUE_PROMPT ? DEFAULT_CONTINUE_LABEL : value;
    }

    function shouldAttachTarget(path) {
      return !(path.startsWith("/api/auth/session") || path.startsWith("/api/logout") || path.startsWith("/api/targets"));
    }

    function builtinLocalTarget() {
      return { id: LOCAL_TARGET_ID, label: "本机", kind: "local", ssh_host: "", ssh_user: "", ssh_port: 22, base_url: "", auth_mode: "key" };
    }

    function loadStoredTargetSecrets() {
      try {
        const raw = JSON.parse(window.sessionStorage.getItem(TARGET_SECRETS_SESSION_KEY) || "{}");
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
        const normalized = {};
        for (const [key, value] of Object.entries(raw)) {
          if (!key || !value || typeof value !== "object") continue;
          const password = String(value.password || "");
          if (password) normalized[String(key)] = { password };
        }
        return normalized;
      } catch (error) {
        return {};
      }
    }

    function saveTargetSecrets() {
      try {
        window.sessionStorage.setItem(TARGET_SECRETS_SESSION_KEY, JSON.stringify(targetSecrets));
      } catch (error) {
      }
    }

    function targetPasswordFor(targetId) {
      const secret = targetSecrets[String(targetId || "").trim()] || null;
      return secret ? String(secret.password || "") : "";
    }

    function currentTarget() {
      return targetItems.find((item) => item.id === currentTargetId) || builtinLocalTarget();
    }

    function withTarget(path, targetOverride = null) {
      if (!shouldAttachTarget(path)) return path;
      const url = new URL(path, window.location.origin);
      const target = targetOverride || currentTarget();
      url.searchParams.set("target", target.id || LOCAL_TARGET_ID);
      if (target.id && target.id !== LOCAL_TARGET_ID) {
        url.searchParams.set("target_label", target.label || target.id);
        url.searchParams.set("ssh_host", target.ssh_host || "");
        url.searchParams.set("ssh_user", target.ssh_user || "");
        url.searchParams.set("ssh_port", String(target.ssh_port || 22));
        url.searchParams.set("base_url", target.base_url || "http://127.0.0.1:8765");
      }
      return `${url.pathname}${url.search}`;
    }

    function buildTargetHeaders(targetOverride = null, passwordOverride = "") {
      const headers = {};
      const target = targetOverride || currentTarget();
      if (!target || target.id === LOCAL_TARGET_ID) return headers;
      const authMode = target.auth_mode === "password" ? "password" : "key";
      headers["X-Target-SSH-Auth"] = authMode;
      if (authMode === "password") {
        const password = String(passwordOverride || targetPasswordFor(target.id) || "");
        if (password) headers["X-Target-Password"] = password;
      }
      return headers;
    }

    function targetFetch(path, options = {}, targetOverride = null, passwordOverride = "") {
      const headers = new Headers(options.headers || {});
      for (const [key, value] of Object.entries(buildTargetHeaders(targetOverride, passwordOverride))) {
        if (value) headers.set(key, value);
      }
      return fetch(withTarget(path, targetOverride), { credentials: "same-origin", ...options, headers });
    }

    function loadSavedTarget() {
      try {
        return String(window.localStorage.getItem(TARGET_STORAGE_KEY) || "").trim() || LOCAL_TARGET_ID;
      } catch (error) {
        return LOCAL_TARGET_ID;
      }
    }

    function saveTarget(value) {
      try {
        window.localStorage.setItem(TARGET_STORAGE_KEY, String(value || LOCAL_TARGET_ID));
      } catch (error) {
      }
    }

    function clearLegacyTargetProfiles() {
      try {
        window.localStorage.removeItem("codex-target-profiles");
      } catch (error) {
      }
    }

    function currentTargetLabel() {
      const found = targetItems.find((item) => item.id === currentTargetId);
      return found ? String(found.label || found.id || currentTargetId) : currentTargetId;
    }

    function renderTargetOptions() {
      targetSelectEl.innerHTML = "";
      for (const item of targetItems) {
        const option = document.createElement("option");
        option.value = String(item.id || "").trim();
        option.textContent = String(item.label || item.id || "unknown");
        targetSelectEl.appendChild(option);
      }
      const validIds = new Set(targetItems.map((item) => String(item.id || "").trim()).filter(Boolean));
      if (!validIds.has(currentTargetId)) currentTargetId = LOCAL_TARGET_ID;
      targetSelectEl.value = currentTargetId;
      saveTarget(currentTargetId);
    }

    function normalizeTargetList(items) {
      if (!Array.isArray(items)) return [];
      return items
        .filter((item) => item && typeof item === "object")
        .map((item) => ({
          id: String(item.id || "").trim(),
          label: String(item.label || item.id || "").trim(),
          kind: String(item.kind || "ssh").trim() || "ssh",
          ssh_host: String(item.ssh_host || "").trim(),
          ssh_user: String(item.ssh_user || "").trim(),
          ssh_port: Number.parseInt(String(item.ssh_port || "22"), 10) || 22,
          base_url: String(item.base_url || "http://127.0.0.1:8765").trim() || "http://127.0.0.1:8765",
          auth_mode: String(item.auth_mode || "key").trim().toLowerCase() === "password" ? "password" : "key",
        }))
        .filter((item) => item.id);
    }

    async function loadTargets(preserveCurrent = true) {
      targetSecrets = loadStoredTargetSecrets();
      clearLegacyTargetProfiles();
      let serverItems = [];
      try {
        const data = await api("/api/targets");
        serverItems = normalizeTargetList(data.targets || []);
      } catch (error) {
        serverItems = [builtinLocalTarget()];
      }
      if (!serverItems.some((item) => item.id === LOCAL_TARGET_ID)) serverItems.unshift(builtinLocalTarget());
      targetItems = serverItems;
      const preferred = preserveCurrent ? currentTargetId : loadSavedTarget();
      currentTargetId = targetItems.some((item) => item.id === preferred) ? preferred : LOCAL_TARGET_ID;
      renderTargetOptions();
    }

    async function testTargetConnection(target, passwordOverride = "") {
      const response = await targetFetch("/api/sessions?limit=1", {}, target, passwordOverride);
      const data = await response.json().catch(() => ({ ok: false, error: "Invalid JSON response" }));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      return data;
    }

    function syncTargetPasswordField() {
      const hidden = targetAuthModeEl.value !== "password";
      targetPasswordFieldEl.dataset.hidden = hidden ? "true" : "false";
      targetPasswordInputEl.disabled = hidden;
      targetPasswordInputEl.required = !hidden;
    }

    function openTargetEditor(target = null) {
      const activeTarget = target && target.id ? target : null;
      targetEditorOriginalId = activeTarget ? String(activeTarget.id || "").trim() : "";
      targetPanelTitleEl.textContent = activeTarget ? `编辑目标机器: ${activeTarget.label || activeTarget.id}` : "添加目标机器";
      targetMetaEl.textContent = "填写目标机 SSH 信息。密码只保存在当前浏览器会话，不写入持久配置。";
      targetLabelInputEl.value = activeTarget ? String(activeTarget.label || "") : "";
      targetHostInputEl.value = activeTarget ? String(activeTarget.ssh_host || "") : "";
      targetUserInputEl.value = activeTarget ? String(activeTarget.ssh_user || "") : "";
      targetPortInputEl.value = activeTarget ? String(activeTarget.ssh_port || 22) : "22";
      targetAuthModeEl.value = activeTarget && activeTarget.auth_mode === "password" ? "password" : "key";
      targetPasswordInputEl.value = activeTarget ? targetPasswordFor(activeTarget.id) : "";
      targetBaseUrlInputEl.value = activeTarget ? String(activeTarget.base_url || "") : "http://127.0.0.1:8765";
      syncTargetPasswordField();
      targetDeleteEl.style.display = activeTarget && activeTarget.id !== LOCAL_TARGET_ID ? "" : "none";
      targetBackdropEl.style.display = "block";
      targetPanelEl.style.display = "block";
      targetLabelInputEl.focus();
      targetLabelInputEl.select();
    }

    function closeTargetEditor() {
      targetEditorOriginalId = "";
      targetDeleteEl.style.display = "none";
      targetBackdropEl.style.display = "none";
      targetPanelEl.style.display = "none";
      targetMetaEl.textContent = "填写目标机 SSH 信息。密码只保存在当前浏览器会话，不写入持久配置。";
    }

    function currentTargetNeedsPassword() {
      const target = currentTarget();
      return !!(target && target.id !== LOCAL_TARGET_ID && target.auth_mode === "password" && !targetPasswordFor(target.id));
    }

    function maybeRequireCurrentTargetPassword() {
      if (!currentTargetNeedsPassword()) return false;
      toast(`目标机器 ${currentTargetLabel()} 需要重新输入 SSH 密码`, true);
      openTargetEditor(currentTarget());
      return true;
    }

    function buildTargetDraftFromEditor() {
      const label = String(targetLabelInputEl.value || "").trim();
      const sshHost = String(targetHostInputEl.value || "").trim();
      const sshUser = String(targetUserInputEl.value || "").trim();
      const sshPort = Number.parseInt(String(targetPortInputEl.value || "22").trim() || "22", 10);
      const authMode = targetAuthModeEl.value === "password" ? "password" : "key";
      const password = String(targetPasswordInputEl.value || "");
      const baseUrl = String(targetBaseUrlInputEl.value || "").trim() || "http://127.0.0.1:8765";
      if (!label) throw new Error("机器显示名不能为空");
      if (!sshHost) throw new Error("SSH 主机不能为空");
      if (!sshUser) throw new Error("SSH 用户不能为空");
      if (!Number.isFinite(sshPort) || sshPort <= 0 || sshPort > 65535) throw new Error("SSH 端口不合法");
      if (authMode === "password" && !password) throw new Error("密码认证模式下必须填写 SSH 密码");
      return {
        target: {
          id: `${sshUser.toLowerCase()}@${sshHost.toLowerCase()}:${sshPort}`,
          label,
          kind: "ssh",
          ssh_host: sshHost,
          ssh_user: sshUser,
          ssh_port: sshPort,
          base_url: baseUrl,
          auth_mode: authMode,
        },
        password,
      };
    }

    function buildTargetPayload(target, password) {
      return {
        label: target.label,
        ssh_host: target.ssh_host,
        ssh_user: target.ssh_user,
        ssh_port: target.ssh_port,
        base_url: target.base_url,
        auth_mode: target.auth_mode,
        ssh_password: password || "",
      };
    }

    function describeTargetCheck(data) {
      const bits = [];
      if (data.recommendation === "legacy_process_conflict") {
        bits.push("远端端口被旧手工实例占用，需先清理旧版再接管");
      } else if (data.recommendation === "port_conflict") {
        bits.push("远端端口已被占用，新服务无法接管");
      } else if (data.recommendation === "port_occupied_without_service") {
        bits.push("远端端口已被其他进程占用");
      } else if (data.recommendation === "upgrade_required_for_ui") {
        bits.push("远端已安装，但接口能力不足以支撑当前 UI，需升级");
      } else if (data.recommendation === "update_recommended") {
        bits.push("远端 codex_manager 已就绪，但版本落后于当前机器");
      } else if (data.recommendation === "ready_unknown_version") {
        bits.push("远端 codex_manager 已就绪，但版本未知");
      } else if (data.api_ready) {
        bits.push("远端 codex_manager 已就绪");
      } else if (data.service_active) {
        bits.push("服务在跑，但 API 暂不可达");
      } else if (data.service_installed) {
        bits.push("远端已安装，但服务未运行");
      } else {
        bits.push("远端尚未安装 codex_manager");
      }
      bits.push(`sudo: ${data.sudo_ok ? "ok" : "缺失"}`);
      bits.push(`python3: ${data.python_ok ? "ok" : "缺失"}`);
      bits.push(`curl: ${data.curl_ok ? "ok" : "缺失"}`);
      bits.push(`tar: ${data.tar_ok ? "ok" : "缺失"}`);
      bits.push(`sessions: ${data.compat_sessions ? "ok" : "缺失"}`);
      bits.push(`remote_sessions: ${data.compat_remote_sessions ? "ok" : "缺失"}`);
      if (data.compat_session_id) bits.push(`events: ${data.compat_events ? "ok" : "缺失"}`);
      if (data.listener_pid) bits.push(`占用进程 PID: ${data.listener_pid}`);
      if (data.listener_command) bits.push(`占用命令: ${data.listener_command}`);
      if (data.remote_version_label) bits.push(`远端版本: ${data.remote_version_label}`);
      if (data.local_version_label) bits.push(`当前版本: ${data.local_version_label}`);
      if (data.api_error) bits.push(`API: ${data.api_error}`);
      return bits.join(" | ");
    }

    async function saveTargetEditor() {
      const { target, password } = buildTargetDraftFromEditor();
      targetMetaEl.textContent = `正在检查远端：${target.label || target.id}`;
      const check = await api("/api/targets/check", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify(buildTargetPayload(target, password))
      });
      targetMetaEl.textContent = describeTargetCheck(check);
      if (!["ready", "ready_unknown_version"].includes(String(check.recommendation || ""))) {
        throw new Error("远端尚未达到可直接接管状态，请先检查或部署更新");
      }
      await api("/api/targets", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify(buildTargetPayload(target, password))
      });
      if (targetEditorOriginalId && targetEditorOriginalId !== target.id) {
        delete targetSecrets[targetEditorOriginalId];
      }
      if (target.auth_mode === "password") {
        targetSecrets[target.id] = { password };
      } else {
        delete targetSecrets[target.id];
      }
      saveTargetSecrets();
      await loadTargets(false);
      currentTargetId = target.id;
      renderTargetOptions();
      closeTargetEditor();
      toast(`已保存机器：${target.label || target.id}`);
    }

    async function checkTargetEditor() {
      const { target, password } = buildTargetDraftFromEditor();
      targetMetaEl.textContent = `正在检查远端：${target.label || target.id}`;
      const data = await api("/api/targets/check", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify(buildTargetPayload(target, password))
      });
      targetMetaEl.textContent = describeTargetCheck(data);
      return data;
    }

    async function bootstrapTargetEditor() {
      const { target, password } = buildTargetDraftFromEditor();
      targetMetaEl.textContent = `正在部署远端：${target.label || target.id}`;
      const data = await api("/api/targets/bootstrap", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify(buildTargetPayload(target, password))
      });
      await api("/api/targets", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify(buildTargetPayload(target, password))
      });
      if (targetEditorOriginalId && targetEditorOriginalId !== target.id) {
        delete targetSecrets[targetEditorOriginalId];
      }
      if (target.auth_mode === "password") {
        targetSecrets[target.id] = { password };
      } else {
        delete targetSecrets[target.id];
      }
      saveTargetSecrets();
      await loadTargets(false);
      currentTargetId = target.id;
      renderTargetOptions();
      closeTargetEditor();
      toast(data.message || `已部署并保存：${target.label || target.id}`);
      return data;
    }

    async function deleteTargetEditor() {
      const targetId = String(targetEditorOriginalId || "").trim();
      if (!targetId || targetId === LOCAL_TARGET_ID) return;
      const target = targetItems.find((item) => item.id === targetId) || currentTarget();
      if (!confirm(`删除目标机器 ${target.label || targetId} ?`)) return;
      const data = await api("/api/targets/delete", {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ target: targetId })
      });
      delete targetSecrets[targetId];
      saveTargetSecrets();
      await loadTargets(false);
      if (currentTargetId === targetId) {
        currentTargetId = LOCAL_TARGET_ID;
        saveTarget(currentTargetId);
      }
      closeTargetEditor();
      toast(data.message || `已删除机器：${target.label || targetId}`);
    }

    async function api(path, options = {}, targetOverride = null, passwordOverride = "") {
      const response = await targetFetch(path, options, targetOverride, passwordOverride);
      const data = await response.json().catch(() => ({ ok: false, error: "Invalid JSON response" }));
      if (response.status === 401 && data.login_url) {
        window.location.href = data.login_url;
        throw new Error("Authentication required");
      }
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      return data;
    }

    function jsonHeaders() {
      const headers = { "Content-Type": "application/json" };
      if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
      return headers;
    }

    async function bootstrapAuth() {
      const data = await api("/api/auth/session");
      csrfToken = data.csrf_token || "";
    }

    function shortId(value) {
      const text = String(value || "");
      if (text.length <= 18) return text;
      return `${text.slice(0, 8)}...${text.slice(-6)}`;
    }

    function shortText(value, limit = 56) {
      const text = String(value || "").trim();
      if (!text) return "";
      if (text.length <= limit) return text;
      return `${text.slice(0, limit - 1)}…`;
    }

    function stateChipLabel(session) {
      return session.archived ? "已归档" : "活动";
    }

    function chip(text, className = "") {
      const span = document.createElement("span");
      span.className = `chip ${className}`.trim();
      span.textContent = text;
      return span;
    }

    function actionButton(text, className, onClick) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = text;
      if (className) button.className = className;
      button.addEventListener("click", onClick);
      return button;
    }

    function renderSourceOptions(sourceOptions) {
      const current = sourceFilterEl.value || "";
      sourceFilterEl.innerHTML = "";
      const allOption = document.createElement("option");
      allOption.value = "";
      allOption.textContent = "全部可见客户端";
      sourceFilterEl.appendChild(allOption);
      for (const item of sourceOptions || []) {
        const label = String(item.label || "").trim();
        if (!label) continue;
        const option = document.createElement("option");
        option.value = String(item.value || label).trim();
        option.textContent = `${label} (${item.count || 0})`;
        sourceFilterEl.appendChild(option);
      }
      sourceFilterEl.value = Array.from(sourceFilterEl.options).some((opt) => opt.value === current) ? current : "";
    }

    function renderGuard(items, warning) {
      state.remoteMarks = new Map((items || []).map((session) => [session.id, session]));
      state.remoteGuardWarning = warning || "";
      guardListEl.innerHTML = "";
      if (!items.length) {
        guardPanelEl.style.display = "none";
        return;
      }
      guardPanelEl.style.display = "block";
      const selectedGuarded = items.some((session) => session.id === state.selectedSessionId);
      const defaultOpen = items.length > 1 || !selectedGuarded;
      guardDetailsEl.open = state.guardTouched ? state.guardOpen : defaultOpen;
      guardSummaryEl.textContent = `Remote Guard · ${items.length} 条网页续跑中${selectedGuarded ? " · 当前会话已在右侧观察" : ""}`;
      guardNoteEl.textContent = state.remoteGuardWarning || "这些会话最近由网页触发继续。回到“等你继续”前，不要从 VS Code 再发消息。";
      for (const session of items) {
        const progress = session.progress || {};
        const item = document.createElement("div");
        item.className = "guard-item";
        item.innerHTML =
          `<strong>${escapeHtml(sessionDisplayName(session))}</strong>` +
          `<div class="mono">${escapeHtml(session.id || "")} | ${escapeHtml(progress.state || "unknown")} | ${escapeHtml(session.remote_mark_started_at || "-")}</div>` +
          `<div>${escapeHtml(progress.reason || "最近由网页触发继续")}</div>` +
          `<div>最近网页消息：${escapeHtml(displayContinuePrompt(session.remote_mark_prompt))}</div>`;
        if (progress.remote_running) {
          const actions = document.createElement("div");
          actions.className = "row-actions";
          actions.style.marginTop = "8px";
          actions.appendChild(actionButton("停止网页任务", "danger", () => stopContinue(session)));
          item.appendChild(actions);
        }
        guardListEl.appendChild(item);
      }
    }

    async function copyText(value, okText) {
      try {
        await navigator.clipboard.writeText(value);
        toast(okText || "已复制");
      } catch (error) {
        toast(error.message || String(error), true);
      }
    }

    function selectedVisibleCount() {
      let count = 0;
      for (const session of state.sessions) {
        if (state.selected.has(session.id)) count += 1;
      }
      return count;
    }

    function updateSelectionUI() {
      const visibleCount = state.sessions.length;
      const checkedCount = selectedVisibleCount();
      selectionMetaEl.textContent = `已选 ${checkedCount} / 当前 ${visibleCount}`;
      selectAllEl.indeterminate = checkedCount > 0 && checkedCount < visibleCount;
      selectAllEl.checked = visibleCount > 0 && checkedCount === visibleCount;
      batchArchiveBtnEl.disabled = checkedCount === 0;
      batchDeleteBtnEl.disabled = checkedCount === 0;
    }

    function reconcileSelection() {
      const visible = new Set((state.sessions || []).map((session) => session.id));
      for (const sessionId of Array.from(state.selected)) {
        if (!visible.has(sessionId)) {
          state.selected.delete(sessionId);
        }
      }
    }

    function escapeHtml(text) {
      return (text || "").replace(/[&<>"']/g, (char) => {
        if (char === "&") return "&amp;";
        if (char === "<") return "&lt;";
        if (char === ">") return "&gt;";
        if (char === "\"") return "&quot;";
        return "&#39;";
      });
    }

    function sanitizeUrl(url) {
      const value = (url || "").trim();
      if (!value) return "";
      if (/^(https?:\/\/|mailto:)/i.test(value)) {
        return value.replace(/"/g, "%22");
      }
      return "";
    }

    function renderInlineMarkdown(text) {
      let raw = (text || "");

      const codeSpans = [];
      raw = raw.replace(/`([^`\n]+)`/g, (_, codeText) => {
        const idx = codeSpans.push(`<code>${escapeHtml(codeText)}</code>`) - 1;
        return `@@INLINE_CODE_${idx}@@`;
      });

      const links = [];
      raw = raw.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, href) => {
        const safeHref = sanitizeUrl(href);
        const safeLabel = escapeHtml(label);
        if (!safeHref) {
          return safeLabel;
        }
        const idx = links.push(
          `<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${safeLabel}</a>`
        ) - 1;
        return `@@LINK_${idx}@@`;
      });

      let output = escapeHtml(raw);
      output = output.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
      output = output.replace(/(^|[^\*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
      output = output.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
      output = output.replace(/\n/g, "<br />");
      output = output.replace(/@@LINK_(\d+)@@/g, (_, idx) => links[Number(idx)] || "");
      output = output.replace(/@@INLINE_CODE_(\d+)@@/g, (_, idx) => codeSpans[Number(idx)] || "");
      return output;
    }

    function renderMarkdown(text) {
      let raw = (text || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      const codeBlocks = [];
      raw = raw.replace(/```([^\n`]*)\n([\s\S]*?)```/g, (_, lang, codeText) => {
        const idx = codeBlocks.push({ lang: (lang || "").trim(), code: codeText }) - 1;
        return `@@CODE_BLOCK_${idx}@@`;
      });

      const lines = raw.split("\n");
      const html = [];
      let index = 0;

      function isBlockStart(value) {
        const line = (value || "").trim();
        if (!line) return false;
        if (/^@@CODE_BLOCK_\d+@@$/.test(line)) return true;
        if (/^#{1,6}\s+/.test(line)) return true;
        if (/^>\s?/.test(line)) return true;
        if (/^\s*[-*+]\s+/.test(line)) return true;
        if (/^\s*\d+\.\s+/.test(line)) return true;
        if (/^(\-{3,}|\*{3,}|_{3,})$/.test(line)) return true;
        return false;
      }

      while (index < lines.length) {
        const line = lines[index];
        const trimmed = (line || "").trim();

        if (!trimmed) {
          index += 1;
          continue;
        }

        const codeMatch = trimmed.match(/^@@CODE_BLOCK_(\d+)@@$/);
        if (codeMatch) {
          const item = codeBlocks[Number(codeMatch[1])];
          if (item) {
            const className = item.lang ? ` class="language-${escapeHtml(item.lang)}"` : "";
            const codeHtml = escapeHtml(String(item.code || "").replace(/\n$/, ""));
            html.push(`<pre><code${className}>${codeHtml}</code></pre>`);
          }
          index += 1;
          continue;
        }

        const headingMatch = line.match(/^(#{1,6})\s+(.*)$/);
        if (headingMatch) {
          const level = headingMatch[1].length;
          html.push(`<h${level}>${renderInlineMarkdown(headingMatch[2].trim())}</h${level}>`);
          index += 1;
          continue;
        }

        if (/^>\s?/.test(trimmed)) {
          const quoteLines = [];
          while (index < lines.length && /^>\s?/.test((lines[index] || "").trim())) {
            quoteLines.push((lines[index] || "").replace(/^>\s?/, ""));
            index += 1;
          }
          html.push(`<blockquote>${renderInlineMarkdown(quoteLines.join("\n"))}</blockquote>`);
          continue;
        }

        if (/^\s*[-*+]\s+/.test(line)) {
          const items = [];
          while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index] || "")) {
            items.push((lines[index] || "").replace(/^\s*[-*+]\s+/, "").trim());
            index += 1;
          }
          html.push(`<ul>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
          continue;
        }

        if (/^\s*\d+\.\s+/.test(line)) {
          const items = [];
          while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index] || "")) {
            items.push((lines[index] || "").replace(/^\s*\d+\.\s+/, "").trim());
            index += 1;
          }
          html.push(`<ol>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ol>`);
          continue;
        }

        if (/^(\-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
          html.push("<hr />");
          index += 1;
          continue;
        }

        const paragraph = [line];
        index += 1;
        while (index < lines.length) {
          const nextLine = lines[index];
          if (!nextLine.trim()) break;
          if (isBlockStart(nextLine)) break;
          paragraph.push(nextLine);
          index += 1;
        }
        html.push(`<p>${renderInlineMarkdown(paragraph.join("\n").trim())}</p>`);
      }

      return html.join("\n");
    }

    function closeHistory() {
      historyBackdropEl.style.display = "none";
      historyPanelEl.style.display = "none";
      historyBodyEl.innerHTML = "";
      historyMetaEl.textContent = "请选择一条会话查看。";
      historyTitleEl.textContent = "历史";
    }

    function renderHistory(session, historyItems, total) {
      historyBackdropEl.style.display = "block";
      historyPanelEl.style.display = "block";
      const sessionName = sessionDisplayName(session);
      historyTitleEl.textContent = `最近 5 轮: ${sessionName}`;
      historyMetaEl.textContent = `会话ID=${session.id} | 显示 ${historyItems.length} 条消息 / ${total} 轮`;
      historyBodyEl.innerHTML = "";
      for (const item of historyItems) {
        const box = document.createElement("div");
        box.className = `history-item ${item.role || "unknown"}`;

        const meta = document.createElement("div");
        meta.className = "history-meta";
        const phaseText = item.phase ? ` / ${item.phase}` : "";
        meta.textContent = `${item.timestamp || "-"} | ${item.role || "-"}${phaseText}`;
        box.appendChild(meta);

        const content = document.createElement("div");
        content.className = "history-markdown";
        content.innerHTML = renderMarkdown(item.text || "");
        box.appendChild(content);

        historyBodyEl.appendChild(box);
      }
      if (!historyItems.length) {
        const empty = document.createElement("div");
        empty.className = "history-meta";
        empty.textContent = "该会话暂无可展示的用户/助手消息。";
        historyBodyEl.appendChild(empty);
      }
    }

    function getSessionTitle(session) {
      return session.session_title || session.title || "";
    }

    function getOfficialTitle(session) {
      return session.official_title || session.thread_name || getSessionTitle(session) || "";
    }

    function getDisplayTitle(session) {
      return session.display_title || session.vscode_display_name || getOfficialTitle(session) || getSessionTitle(session) || session.id || "-";
    }

    function buildTitleDetails(session) {
      const parts = [];
      const displayTitle = getDisplayTitle(session);
      const sessionTitle = getSessionTitle(session);
      const officialTitle = getOfficialTitle(session);
      if (session.session_title_is_noisy) {
        parts.push("本地标题已隐藏（疑似脏值）");
      } else if (sessionTitle && sessionTitle !== displayTitle) {
        parts.push(`本地标题: ${sessionTitle}`);
      }
      if (session.official_title_is_noisy) {
        parts.push("官方标题已隐藏（疑似脏值）");
      } else if (officialTitle && officialTitle !== displayTitle && officialTitle !== sessionTitle) {
        parts.push(`官方标题: ${officialTitle}`);
      }
      return parts;
    }

    function sessionDisplayName(session) {
      return String(session.alias || getDisplayTitle(session) || shortId(session.id)).trim() || shortId(session.id);
    }

    function buildDerivationDetails(session) {
      const parentId = String(session.parent_session_id || "").trim();
      if (!parentId) return [];
      const parts = [];
      parts.push(`派生自: ${session.parent_display_title || shortId(parentId)}`);
      const traits = [];
      if (session.subagent_role) traits.push(session.subagent_role);
      if (session.subagent_nickname) traits.push(session.subagent_nickname);
      if (Number(session.subagent_depth || 0) > 0) traits.push(`深度 ${session.subagent_depth}`);
      if (traits.length) parts.push(`子 Agent=${traits.join(" / ")}`);
      parts.push(`父会话ID=${shortId(parentId)}`);
      return parts;
    }

    function sessionSecondaryText(session) {
      const parts = [];
      const displayTitle = getDisplayTitle(session);
      if (session.alias && displayTitle && session.alias !== displayTitle) {
        parts.push(`显示标题: ${displayTitle}`);
      }
      parts.push(...buildTitleDetails(session));
      parts.push(...buildDerivationDetails(session));
      parts.push(session.id);
      return parts.join(" | ");
    }

    function buildSourceDetails(session) {
      const parts = [];
      parts.push(...buildDerivationDetails(session));
      if (session.source_filter_key) {
        parts.push(`分类=${session.source_filter_key}`);
      }
      if (session.client_source) {
        parts.push(`DB=${shortText(session.client_source, 42)}`);
      }
      if (session.session_source && session.session_source !== session.client_source) {
        parts.push(`文件=${shortText(session.session_source, 42)}`);
      }
      if (!parts.length) {
        parts.push(`文件=${shortText(session.session_source || session.source || "-", 42)}`);
      }
      if (session.originator) {
        parts.push(`originator=${session.originator}`);
      }
      return parts;
    }

    function currentSourceChoice(session) {
      const normalized = String(session.source_filter_key || "").trim().toLowerCase();
      const preset = SOURCE_PRESETS.find((item) => item.value !== "__custom__" && item.value === normalized);
      return preset ? preset.value : "__custom__";
    }

    function currentSourceValue() {
      if (state.sourceEditor.choice === "__custom__") {
        return String(state.sourceEditor.custom || "").trim();
      }
      return String(state.sourceEditor.choice || "").trim();
    }

    function renderSourceEditor() {
      const session = state.sourceEditor.session;
      if (!session) return;
      sourceChoicesEl.innerHTML = "";
      for (const preset of SOURCE_PRESETS) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `source-choice${state.sourceEditor.choice === preset.value ? " active" : ""}`;
        button.addEventListener("click", () => {
          state.sourceEditor.choice = preset.value;
          if (preset.value === "__custom__" && !state.sourceEditor.custom) {
            state.sourceEditor.custom = String(
              session.client_source || session.source || session.session_source || ""
            ).trim();
          } else if (preset.value !== "__custom__") {
            state.sourceEditor.custom = "";
          }
          renderSourceEditor();
        });

        const title = document.createElement("div");
        title.className = "source-choice-title";
        title.textContent = preset.label;
        button.appendChild(title);

        const note = document.createElement("div");
        note.className = "source-choice-note";
        note.textContent = preset.note;
        button.appendChild(note);

        sourceChoicesEl.appendChild(button);
      }

      const customMode = state.sourceEditor.choice === "__custom__";
      sourceCustomInputEl.disabled = !customMode;
      sourceCustomInputEl.value = state.sourceEditor.custom || "";
      const nextValue = currentSourceValue();
      const currentValue = String(session.client_source || session.source || session.session_source || "").trim() || "(none)";
      sourcePreviewEl.textContent = customMode
        ? `将写入自定义 source: ${nextValue || "(未填写)"}。只有你明确知道目标客户端如何识别时才建议这么做。`
        : `将把 source 设为 ${nextValue}。这会同步刷新 state_5.sqlite 的 source/title，并追加 session_index.jsonl 记录，优先修对应客户端的可见性。当前值: ${currentValue}。`;
    }

    function openSourceEditor(session) {
      state.sourceEditor.session = session;
      state.sourceEditor.choice = currentSourceChoice(session);
      state.sourceEditor.custom = state.sourceEditor.choice === "__custom__"
        ? String(session.client_source || session.source || session.session_source || "").trim()
        : "";
      sourcePanelTitleEl.textContent = `设置可见客户端: ${sessionDisplayName(session)}`;
      sourceMetaEl.textContent = `当前显示标题: ${getDisplayTitle(session)} | 会话ID=${session.id}`;
      sourceBackdropEl.style.display = "block";
      sourcePanelEl.style.display = "block";
      renderSourceEditor();
      if (state.sourceEditor.choice === "__custom__") {
        sourceCustomInputEl.focus();
        sourceCustomInputEl.select();
      }
    }

    function closeSourceEditor() {
      state.sourceEditor = { session: null, choice: "", custom: "" };
      sourceBackdropEl.style.display = "none";
      sourcePanelEl.style.display = "none";
      sourceChoicesEl.innerHTML = "";
      sourceCustomInputEl.value = "";
      sourceCustomInputEl.disabled = true;
      sourcePreviewEl.textContent = "请选择一个预设或输入自定义 source。";
    }

    async function saveSourceEditor() {
      const session = state.sourceEditor.session;
      if (!session) return;
      const value = currentSourceValue();
      if (!value) return toast("source 不能为空", true);
      try {
        await api("/api/set_source", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id, source: value }),
        });
        closeSourceEditor();
        await loadSessions();
        toast(`已更新可见客户端：${value}`);
      } catch (error) {
        toast(error.message, true);
      }
    }

    function sessionUpdatedText(session) {
      return String(session.updated_at || session.timestamp || "-");
    }

    function sessionModelText(session) {
      return `${session.model || "-"} / ${session.effort || "-"}`;
    }

    function sessionRemoteMark(session) {
      return state.remoteMarks.get(session.id) || null;
    }

	    function sessionRemoteRunning(session) {
	      const mark = sessionRemoteMark(session);
	      return Boolean(mark && mark.progress && mark.progress.remote_running);
	    }

	    function progressStateLabel(value) {
	      const text = String(value || "").trim();
	      const labels = {
	        running: "运行中",
	        queued: "排队中",
	        waiting: "待继续",
	        aborted: "已中断",
	        unknown: "未知",
	      };
	      return labels[text] || text || "未知";
	    }

	    function attentionStateLabel(value) {
	      const text = String(value || "").trim();
	      const labels = {
	        active: "自动推进",
	        needs_attention: "需要介入",
	        completed: "已完成",
	        check: "停下待看",
	        unknown: "待判断",
	      };
	      return labels[text] || text || "待判断";
	    }

	    function progressChipClass(value) {
	      const text = String(value || "").trim();
	      if (text === "running" || text === "queued" || text === "active") return "accent";
	      if (text === "aborted" || text === "needs_attention") return "danger";
	      if (text === "waiting" || text === "completed" || text === "check") return "warn";
	      return "";
	    }

	    function selectedSession() {
	      return state.sessions.find((session) => session.id === state.selectedSessionId) || null;
	    }

	    function stopLivePolling() {
	      if (state.livePollTimer) {
	        window.clearTimeout(state.livePollTimer);
	        state.livePollTimer = null;
	      }
	    }

	    function scheduleLivePolling() {
	      stopLivePolling();
	      if (!state.selectedSessionId) return;
	      state.livePollTimer = window.setTimeout(() => {
	        loadLiveEvents({ reset: false }).catch((error) => {
	          console.error(error);
	          toast(error.message || String(error), true);
	        });
	      }, 1800);
	    }

	    function stopSessionRefresh() {
	      if (state.sessionRefreshTimer) {
	        window.clearTimeout(state.sessionRefreshTimer);
	        state.sessionRefreshTimer = null;
	      }
	    }

	    function scheduleSessionRefresh() {
	      stopSessionRefresh();
	      state.sessionRefreshTimer = window.setTimeout(() => {
	        if (document.hidden || maybeRequireCurrentTargetPassword()) {
	          scheduleSessionRefresh();
	          return;
	        }
	        loadSessions().catch((error) => {
	          console.error(error);
	        });
	      }, 20000);
	    }

	    function ensureSelectedSession() {
	      const ids = new Set((state.sessions || []).map((session) => session.id));
	      if (state.selectedSessionId && ids.has(state.selectedSessionId)) return;
	      const first = state.sessions[0] || null;
	      state.selectedSessionId = first ? first.id : "";
	      state.liveSessionId = "";
	      state.liveCursor = 0;
	      state.liveEvents = [];
	      state.liveOpenDetailKeys = new Set();
	    }

	    function replaceSessionSnapshot(nextSession, progress, updatedAt) {
	      const idx = state.sessions.findIndex((item) => item.id === nextSession.id);
	      if (idx < 0) return;
	      const merged = { ...state.sessions[idx], ...nextSession };
	      if (progress) merged.progress = progress;
	      if (updatedAt) merged.updated_at = updatedAt;
	      state.sessions[idx] = merged;
	    }

	    function selectSession(sessionId, { resetLive = true } = {}) {
	      const nextId = String(sessionId || "").trim();
	      if (!nextId || nextId === state.selectedSessionId) return;
	      state.selectedSessionId = nextId;
	      if (resetLive) {
	        state.liveSessionId = "";
	        state.liveCursor = 0;
	        state.liveEvents = [];
	        state.liveOpenDetailKeys = new Set();
	      }
	      renderSessionViews();
	      loadLiveEvents({ reset: true }).catch((error) => {
	        console.error(error);
	        toast(error.message || String(error), true);
	      });
	    }

	    function compactInlineText(text, maxLen = 170) {
	      const value = String(text || "").replace(/\s+/g, " ").trim();
	      if (!value) return "";
	      if (value.length <= maxLen) return value;
	      return `${value.slice(0, Math.max(0, maxLen - 1)).trimEnd()}…`;
	    }

	    function sessionPreviewText(session) {
	      const progress = session.progress || {};
	      const raw = String(progress.preview || progress.last_assistant_preview || progress.last_user_preview || sessionSecondaryText(session)).trim();
	      return compactInlineText(raw, state.selectedSessionId === session.id ? 190 : 150);
	    }

	    function sessionStatusSummary(session) {
	      const progress = session.progress || {};
	      const parts = [
	        progressStateLabel(progress.state),
	        attentionStateLabel(progress.attention_state),
	      ];
	      if (sessionRemoteRunning(session)) parts.push("网页续跑中");
	      return parts.join(" · ");
	    }

	    function isNearBottom(el, slack = 96) {
	      if (!el) return true;
	      return (el.scrollHeight - el.scrollTop - el.clientHeight) <= slack;
	    }

	    function normalizedEventText(event) {
	      const text = String(event.text || event.preview || "").trim();
	      if (!text) return "";
	      return text.replace(/\s+/g, " ").trim();
	    }

	    function normalizedLooseText(value) {
	      return String(value || "").replace(/\s+/g, " ").trim();
	    }

	    function eventDuplicateBucket(event) {
	      const kind = String(event.kind || "").trim();
	      if (kind === "commentary" || kind === "assistant_message") return "assistant_surface";
	      if (kind === "user_message") return "user_surface";
	      return kind;
	    }

	    function eventDuplicatePriority(event) {
	      const kind = String(event.kind || "").trim();
	      if (kind === "commentary") return 30;
	      if (kind === "assistant_message") return 20;
	      if (kind === "user_message") return 10;
	      return 0;
	    }

	    function eventsAreDuplicates(previous, current) {
	      if (!previous || !current) return false;
	      if (eventDuplicateBucket(previous) !== eventDuplicateBucket(current)) return false;
	      const previousTurn = String(previous.turn_id || "").trim();
	      const currentTurn = String(current.turn_id || "").trim();
	      if (previousTurn && currentTurn && previousTurn !== currentTurn) return false;
	      const previousTimestamp = String(previous.timestamp || "").trim().slice(0, 19);
	      const currentTimestamp = String(current.timestamp || "").trim().slice(0, 19);
	      if (previousTimestamp && currentTimestamp && previousTimestamp !== currentTimestamp) return false;
	      const previousText = normalizedEventText(previous);
	      const currentText = normalizedEventText(current);
	      return Boolean(previousText && currentText && previousText === currentText);
	    }

	    function dedupeSessionEvents(events) {
	      const deduped = [];
	      for (const event of events || []) {
	        const previous = deduped[deduped.length - 1];
	        if (previous && eventsAreDuplicates(previous, event)) {
	          if (eventDuplicatePriority(event) > eventDuplicatePriority(previous)) {
	            deduped[deduped.length - 1] = event;
	          }
	          continue;
	        }
	        deduped.push(event);
	      }
	      return deduped;
	    }

	    function eventStorageKey(sessionId, event) {
	      return [
	        String(sessionId || ""),
	        String(event.kind || ""),
	        String(event.timestamp || ""),
	        String(event.turn_id || ""),
	        String(event.call_id || ""),
	        String(event.command || ""),
	        String(event.preview || "").slice(0, 120),
	      ].join("|");
	    }

	    function renderFleetMeta(session, root) {
	      const progress = session.progress || {};
	      const meta = document.createElement("div");
	      meta.className = "fleet-card-meta";
	      if (state.selectedSessionId === session.id) meta.appendChild(chip("当前观察", "accent"));
	      meta.appendChild(chip(session.source_short_label || session.source_label || "-", "accent"));
	      meta.appendChild(chip(progressStateLabel(progress.state), progressChipClass(progress.state)));
	      meta.appendChild(chip(attentionStateLabel(progress.attention_state), progressChipClass(progress.attention_state)));
	      if (sessionRemoteRunning(session)) meta.appendChild(chip("网页续跑中", "warn"));
	      root.appendChild(meta);
	    }

	    function shouldShowRecentTools(session) {
	      const progress = session.progress || {};
	      if (!(progress.recent_tools && progress.recent_tools.length)) return false;
	      if (sessionRemoteRunning(session)) return true;
	      if (state.selectedSessionId === session.id) return true;
	      return progress.state === "running" || progress.attention_state === "needs_attention";
	    }

	    function eventBodyHtml(event) {
	      const text = String(event.text || "").trim();
	      if (!text) return "";
	      if (["tool_call", "tool_output", "token_count"].includes(String(event.kind || ""))) {
	        return `<pre>${escapeHtml(text)}</pre>`;
	      }
	      return renderMarkdown(text);
	    }

	    function shouldCollapseEventBody(event) {
	      const kind = String(event.kind || "");
	      const text = String(event.text || "").trim();
	      if (!text) return false;
	      if (kind === "tool_output" || kind === "token_count") return true;
	      if (kind === "tool_call") return true;
	      if (kind === "reasoning") return true;
	      return text.length > 520 || text.split(/\n/).length > 10;
	    }

	    function shouldShowEventPreview(event, collapseByDefault) {
	      const preview = normalizedLooseText(event.preview || "");
	      if (!preview) return false;
	      if (collapseByDefault) return true;
	      const text = normalizedLooseText(event.text || "");
	      if (!text) return true;
	      if (preview === text) return false;
	      if (text.startsWith(preview) && text.length <= preview.length + 48) return false;
	      return true;
	    }

	    function eventDetailLabel(event) {
	      const kind = String(event.kind || "");
	      if (kind === "tool_output") return "展开工具输出";
	      if (kind === "tool_call") return "展开参数";
	      if (kind === "token_count") return "展开 token 明细";
	      if (kind === "reasoning") return "展开 reasoning 摘要";
	      return "展开详细内容";
	    }

	    function renderLivePanel() {
	      const previousScrollTop = liveEventsEl.scrollTop;
	      const shouldStick = state.liveForceScrollBottom || state.liveStickToBottom || isNearBottom(liveEventsEl);
	      const session = selectedSession();
	      if (!session) {
	        liveSessionTitleEl.textContent = "没有可观察的会话";
	        liveSessionMetaEl.textContent = "当前筛选条件下没有 session。";
	        liveSessionUpdatedEl.textContent = "-";
	        liveStatusRowEl.innerHTML = "";
	        liveActionsEl.innerHTML = "";
	        liveEventsEl.innerHTML = '<div class="live-empty">调整筛选条件，或者切到别的目标机器。</div>';
	        liveComposerHelpEl.textContent = "当前没有选中的 session";
	        for (const el of [liveSendBtnEl, liveContinueBtnEl, liveStopBtnEl, liveRefreshBtnEl, liveHistoryBtnEl, liveResumeCmdBtnEl, liveSetTitleBtnEl, liveSetSourceBtnEl, liveSetWorkdirBtnEl, liveComposerInputEl]) {
	          el.disabled = true;
	        }
	        return;
	      }
	      const progress = session.progress || {};
	      liveSessionTitleEl.textContent = sessionDisplayName(session);
	      liveSessionMetaEl.textContent = `${session.id} | ${sessionModelText(session)} | ${session.cwd_display || session.cwd || "-"}`;
	      liveSessionUpdatedEl.textContent = sessionUpdatedText(session);
	      liveStatusRowEl.innerHTML = "";
	      if (sessionRemoteRunning(session)) liveStatusRowEl.appendChild(chip("网页续跑中", "warn"));
	      liveStatusRowEl.appendChild(chip(progressStateLabel(progress.state), progressChipClass(progress.state)));
	      liveStatusRowEl.appendChild(chip(attentionStateLabel(progress.attention_state), progressChipClass(progress.attention_state)));
	      liveStatusRowEl.appendChild(chip(session.source_short_label || session.source_label || "-", "accent"));
	      if (session.archived) liveStatusRowEl.appendChild(chip("已归档", "warn"));
	      if (progress.last_assistant_phase) liveStatusRowEl.appendChild(chip(`phase: ${progress.last_assistant_phase}`));

	      liveActionsEl.innerHTML = "";
	      const notes = [];
	      if (progress.reason) notes.push(`状态: ${progress.reason}`);
	      if (progress.attention_reason) notes.push(`判断: ${progress.attention_reason}`);
	      if (progress.recent_tools && progress.recent_tools.length) {
	        const visibleTools = progress.recent_tools.slice(0, 3);
	        const hiddenCount = Math.max(0, progress.recent_tools.length - visibleTools.length);
	        notes.push(`最近工具: ${visibleTools.join(", ")}${hiddenCount ? ` +${hiddenCount}` : ""}`);
	      }
	      if (!notes.length) notes.push("这个面板会持续滚动显示当前会话的 event timeline。");
	      for (const note of notes.slice(0, 3)) {
	        const pill = document.createElement("div");
	        pill.className = "status-pill";
	        pill.textContent = note;
	        liveActionsEl.appendChild(pill);
	      }

	      liveEventsEl.innerHTML = "";
	      if (!state.liveEvents.length) {
	        const empty = document.createElement("div");
	        empty.className = "live-empty";
	        empty.textContent = `还没有抓到近期事件，稍等自动刷新。这个窗口默认只保留最近 ${LIVE_EVENT_WINDOW} 条事件。`;
	        liveEventsEl.appendChild(empty);
	      } else {
	        for (const event of state.liveEvents) {
	          const kind = String(event.kind || "");
	          const bodyHtml = eventBodyHtml(event);
	          const collapseByDefault = shouldCollapseEventBody(event);
	          const showPreview = shouldShowEventPreview(event, collapseByDefault);
	          const detailKey = eventStorageKey(session.id, event);
	          const card = document.createElement("div");
	          card.className = `event-card kind-${kind || "unknown"}`;
	          const head = document.createElement("div");
	          head.className = "event-head";
	          const title = document.createElement("div");
	          title.className = "event-title";
	          title.textContent = String(event.title || event.kind || "event");
	          head.appendChild(title);
	          const time = document.createElement("div");
	          time.className = "event-time mono";
	          time.textContent = String(event.timestamp || "-");
	          head.appendChild(time);
	          card.appendChild(head);
	          if (showPreview) {
	            const preview = document.createElement("div");
	            preview.className = "event-preview";
	            preview.textContent = String(event.preview || "");
	            card.appendChild(preview);
	          }
	          const tags = [];
	          if (event.tool_name) tags.push(chip(String(event.tool_name), "accent"));
	          if (event.call_id) tags.push(chip(`call ${String(event.call_id).slice(0, 8)}`, ""));
	          if (event.exit_code !== undefined && event.exit_code !== null) {
	            tags.push(chip(`exit ${event.exit_code}`, Number(event.exit_code) === 0 ? "accent" : "danger"));
	          }
	          if (event.chunk_id) tags.push(chip(`chunk ${String(event.chunk_id).slice(0, 8)}`, ""));
	          if (tags.length) {
	            const tagRow = document.createElement("div");
	            tagRow.className = "event-tags";
	            for (const node of tags) tagRow.appendChild(node);
	            card.appendChild(tagRow);
	          }
	          if (bodyHtml) {
	            const body = document.createElement("div");
	            body.className = "event-body";
	            body.innerHTML = bodyHtml;
	            if (collapseByDefault) {
	              const details = document.createElement("details");
	              details.className = "event-details";
	              details.open = state.liveOpenDetailKeys.has(detailKey);
	              details.addEventListener("toggle", () => {
	                if (details.open) {
	                  state.liveOpenDetailKeys.add(detailKey);
	                } else {
	                  state.liveOpenDetailKeys.delete(detailKey);
	                }
	              });
	              const summary = document.createElement("summary");
	              summary.className = "event-detail-summary";
	              summary.textContent = eventDetailLabel(event);
	              details.appendChild(summary);
	              details.appendChild(body);
	              card.appendChild(details);
	            } else {
	              card.appendChild(body);
	            }
	          }
	          liveEventsEl.appendChild(card);
	        }
	        requestAnimationFrame(() => {
	          if (shouldStick) {
	            liveEventsEl.scrollTop = liveEventsEl.scrollHeight;
	            state.liveStickToBottom = true;
	          } else {
	            liveEventsEl.scrollTop = previousScrollTop;
	          }
	          state.liveForceScrollBottom = false;
	        });
	      }

	      liveComposerHelpEl.textContent = `${sessionStatusSummary(session)} · 窗口保留最近 ${LIVE_EVENT_WINDOW} 条事件`;
	      liveSendBtnEl.disabled = false;
	      liveContinueBtnEl.disabled = false;
	      liveStopBtnEl.disabled = !sessionRemoteRunning(session);
	      liveRefreshBtnEl.disabled = false;
	      liveHistoryBtnEl.disabled = false;
	      liveResumeCmdBtnEl.disabled = false;
	      liveSetTitleBtnEl.disabled = false;
	      liveSetSourceBtnEl.disabled = false;
	      liveSetWorkdirBtnEl.disabled = false;
	      liveComposerInputEl.disabled = false;
	    }

	    async function loadLiveEvents({ reset = false } = {}) {
	      const session = selectedSession();
	      if (!session) {
	        stopLivePolling();
	        return;
	      }
	      if (maybeRequireCurrentTargetPassword()) return;
	      if (state.livePollInFlight) return;
	      state.livePollInFlight = true;
	      const activeSessionId = session.id;
	      try {
	        const limit = reset || state.liveSessionId !== activeSessionId || !state.liveCursor
	          ? LIVE_RESET_LIMIT
	          : LIVE_DELTA_LIMIT;
	        const base = `/api/events?session=${encodeURIComponent(activeSessionId)}&limit=${encodeURIComponent(limit)}`;
	        const path = reset || state.liveSessionId !== activeSessionId || !state.liveCursor
	          ? base
	          : `${base}&cursor=${encodeURIComponent(state.liveCursor)}`;
	        const data = await api(path);
	        if (state.selectedSessionId !== activeSessionId) return;
	        if (reset || data.reset || state.liveSessionId !== activeSessionId) {
	          state.liveEvents = dedupeSessionEvents(data.events || []);
	          state.liveForceScrollBottom = true;
	          state.liveStickToBottom = true;
	        } else if ((data.events || []).length) {
	          state.liveEvents = dedupeSessionEvents(state.liveEvents.concat(data.events || [])).slice(-LIVE_EVENT_WINDOW);
	        }
	        state.liveSessionId = activeSessionId;
	        state.liveCursor = Number(data.cursor || 0);
	        if (data.session) {
	          replaceSessionSnapshot(data.session, data.progress || null, data.updated_at || "");
	        }
	        renderSessionViews();
	      } finally {
	        state.livePollInFlight = false;
	        scheduleLivePolling();
	      }
	    }

	    function setSessionSelected(sessionId, selected) {
	      if (selected) {
	        state.selected.add(sessionId);
	      } else {
        state.selected.delete(sessionId);
      }
    }

    function makeSelectionPill(session, labelText = "选择") {
      const label = document.createElement("label");
      label.className = "select-pill";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selected.has(session.id);
      checkbox.addEventListener("change", () => {
        setSessionSelected(session.id, checkbox.checked);
        renderSessionViews();
      });
      label.appendChild(checkbox);
      if (String(labelText || "").trim()) {
        label.appendChild(document.createTextNode(labelText));
      }
      return label;
    }

    function infoTile(label, value, monospace = false) {
      const tile = document.createElement("div");
      tile.className = "info-tile";

      const key = document.createElement("div");
      key.className = "info-label";
      key.textContent = label;
      tile.appendChild(key);

      const content = document.createElement("div");
      content.className = `info-value${monospace ? " mono" : ""}`;
      content.textContent = value || "-";
      tile.appendChild(content);
      return tile;
    }

    function richInfoTile(label, nodes) {
      const tile = document.createElement("div");
      tile.className = "info-tile rich";

      const key = document.createElement("div");
      key.className = "info-label";
      key.textContent = label;
      tile.appendChild(key);

      const content = document.createElement("div");
      content.className = "info-value";
      for (const node of nodes || []) {
        if (node) content.appendChild(node);
      }
      tile.appendChild(content);
      return tile;
    }

    function buildActionGroup(label, buttons) {
      const filtered = buttons.filter(Boolean);
      if (!filtered.length) return null;
      const group = document.createElement("div");
      group.className = "action-group";
      const title = document.createElement("div");
      title.className = "action-group-label";
      title.textContent = label;
      group.appendChild(title);
      const body = document.createElement("div");
      body.className = `action-group-body${filtered.length === 1 ? " single" : ""}`;
      for (const button of filtered) body.appendChild(button);
      group.appendChild(body);
      return group;
    }

    function buildActionStack(session, options = {}) {
      const advancedOnly = Boolean(options.advancedOnly);
      const stack = document.createElement("div");
      stack.className = "action-stack";
      const primary = buildActionGroup(advancedOnly ? "追加续跑" : "续跑", advancedOnly ? [
        sessionRemoteRunning(session) ? actionButton("停止续跑", "danger", () => stopContinue(session)) : null,
        actionButton("人工补一句", "", () => customContinueSession(session)),
      ] : [
        actionButton("最近 5 轮", "", () => viewHistory(session)),
        sessionRemoteRunning(session) ? actionButton("停止续跑", "danger", () => stopContinue(session)) : null,
        actionButton(DEFAULT_CONTINUE_LABEL, "primary", () => continueSession(session)),
        actionButton("人工补一句", "", () => customContinueSession(session)),
      ]);
      const edit = buildActionGroup("元数据", [
        actionButton("设标题", "", () => setSessionTitle(session)),
        actionButton("清自定义标题", "", () => clearSessionTitle(session)),
        actionButton("设可见客户端", "", () => openSourceEditor(session)),
        actionButton("设工作目录", "", () => setSessionWorkdir(session)),
      ]);
      const maintenance = buildActionGroup("维护", [
        actionButton("复制终端恢复命令", "", () => copyResumeCmd(session)),
        actionButton("归档会话", "warn", () => archiveSession(session)),
        actionButton("删除会话", "danger", () => deleteSession(session)),
      ]);
      for (const group of [primary, edit, maintenance]) {
        if (group) stack.appendChild(group);
      }
      return stack;
    }

    function derivationTraits(session) {
      const traits = [];
      if (session.subagent_role) traits.push(session.subagent_role);
      if (session.subagent_nickname) traits.push(session.subagent_nickname);
      if (Number(session.subagent_depth || 0) > 0) traits.push(`深度 ${session.subagent_depth}`);
      return traits;
    }

    function parentSessionId(session) {
      return String(session.parent_session_id || "").trim();
    }

    function parentSessionLabel(session) {
      const parentId = parentSessionId(session);
      return String(session.parent_display_title || "").trim() || shortId(parentId);
    }

    function buildCompactTitleMeta(session) {
      const parentId = parentSessionId(session);
      if (parentId) {
        const wrap = document.createElement("div");
        wrap.className = "inline-meta";

        const note = document.createElement("span");
        note.className = "inline-note";
        note.textContent = "派生自";
        wrap.appendChild(note);

        const jump = actionButton(parentSessionLabel(session), "link-button", () => focusSessionById(parentId));
        wrap.appendChild(jump);

        const traits = derivationTraits(session);
        if (traits.length) wrap.appendChild(chip(traits.join(" / ")));
        return wrap;
      }

      const titleDetails = buildTitleDetails(session);
      if (!titleDetails.length) return null;
      const subtle = document.createElement("div");
      subtle.className = "cell-subtle truncate-line";
      subtle.textContent = titleDetails[0];
      subtle.title = titleDetails.join(" | ");
      return subtle;
    }

    function buildCompactActionBar(session) {
      const bar = document.createElement("div");
      bar.className = "compact-actions";
      bar.appendChild(actionButton("最近 5 轮", "", () => viewHistory(session)));
      if (sessionRemoteRunning(session)) {
        bar.appendChild(actionButton("停止续跑", "danger", () => stopContinue(session)));
      } else {
        bar.appendChild(actionButton(DEFAULT_CONTINUE_LABEL, "primary", () => continueSession(session)));
      }
      const expanded = state.expandedSessionId === session.id;
      bar.appendChild(actionButton(expanded ? "收起操作" : "更多操作", "", () => toggleSessionDetails(session)));
      return bar;
    }

    function buildSessionStatusText(session) {
      const parts = [stateChipLabel(session)];
      if (session.alias) parts.push("有别名");
      if (sessionRemoteRunning(session)) parts.push("网页续跑中");
      return parts.join(" | ");
    }

    function buildRelationTile(session) {
      const parentId = parentSessionId(session);
      if (!parentId) return null;
      const rows = [];

      const summary = document.createElement("div");
      summary.textContent = `父会话: ${parentSessionLabel(session)}`;
      rows.push(summary);

      const meta = document.createElement("div");
      meta.className = "inline-note mono";
      meta.textContent = `父会话ID=${parentId}`;
      rows.push(meta);

      const traits = derivationTraits(session);
      if (traits.length) {
        const traitRow = document.createElement("div");
        traitRow.className = "inline-meta";
        traitRow.appendChild(chip(traits.join(" / ")));
        rows.push(traitRow);
      }

      const jumpRow = document.createElement("div");
      jumpRow.className = "inline-meta";
      jumpRow.appendChild(actionButton("跳到父会话", "link-button", () => focusSessionById(parentId)));
      rows.push(jumpRow);

      return richInfoTile("派生关系", rows);
    }

    function buildQuickUtilityRow(session) {
      const row = document.createElement("div");
      row.className = "session-utility-row";
      row.appendChild(actionButton("复制会话 ID", "", () => copyText(session.id, `已复制会话 ID: ${session.id}`)));

      const cwdRaw = String(session.cwd_raw || session.cwd || "").trim();
      if (cwdRaw) {
        row.appendChild(actionButton("复制工作目录", "", () => copyText(cwdRaw, `已复制工作目录: ${cwdRaw}`)));
      }

      const sourceRaw = String(session.client_source || session.session_source || session.source || "").trim();
      if (sourceRaw) {
        row.appendChild(actionButton("复制 source", "", () => copyText(sourceRaw, `已复制 source: ${sourceRaw}`)));
      }

      const parentId = parentSessionId(session);
      if (parentId) {
        row.appendChild(actionButton("跳到父会话", "", () => focusSessionById(parentId)));
      }

      return row;
    }

    function buildContextGrid(session) {
      const grid = document.createElement("div");
      grid.className = "session-context-grid";

      const relationTile = buildRelationTile(session);
      if (relationTile) {
        grid.appendChild(relationTile);
      }

      const titleDetails = buildTitleDetails(session);
      if (titleDetails.length) {
        grid.appendChild(infoTile("标题差异", titleDetails.join(" | ")));
      }

      const sourceDetails = buildSourceDetails(session).join(" | ");
      if (sourceDetails) {
        grid.appendChild(infoTile("source", sourceDetails));
      }

      const cwdRaw = String(session.cwd_raw || session.cwd || "").trim();
      if (cwdRaw) {
        grid.appendChild(infoTile("工作目录", cwdRaw, true));
      }

      const slackValue = (session.slack_threads || []).join(", ");
      if (slackValue) {
        grid.appendChild(infoTile("Slack 线程", slackValue, true));
      }

      return grid;
    }

    function buildExpandedDetailPanel(session) {
      const panel = document.createElement("div");
      panel.className = "session-detail-panel";

      const head = document.createElement("div");
      head.className = "session-detail-head";

      const headCopy = document.createElement("div");
      const title = document.createElement("h3");
      title.className = "session-detail-title";
      title.textContent = "会话工具箱";
      headCopy.appendChild(title);

      const subtitle = document.createElement("div");
      subtitle.className = "session-detail-subtitle";
      subtitle.textContent = `${sessionDisplayName(session)} · ${shortId(session.id)} · ${session.source_short_label || session.source_label || "-"} · ${stateChipLabel(session)}`;
      headCopy.appendChild(subtitle);
      head.appendChild(headCopy);

      const headActions = document.createElement("div");
      headActions.className = "compact-actions";
      headActions.appendChild(actionButton("收起操作", "", () => toggleSessionDetails(session)));
      head.appendChild(headActions);
      panel.appendChild(head);

      const layout = document.createElement("div");
      layout.className = "session-detail-layout";

      layout.appendChild(buildQuickUtilityRow(session));

      const actions = document.createElement("div");
      actions.className = "session-detail-actions";
      actions.appendChild(buildActionStack(session, { advancedOnly: true }));
      layout.appendChild(actions);

      const contextGrid = buildContextGrid(session);
      if (contextGrid.children.length) {
        layout.appendChild(contextGrid);
      }

      panel.appendChild(layout);
      return panel;
    }

    function scrollSessionIntoView(sessionId) {
      const selector = `[data-session-id="${sessionId}"]`;
      const row = rowsEl.querySelector(selector);
      const card = cardsEl.querySelector(selector);
      const target = row || card;
      if (!target) return;
      target.scrollIntoView({ block: "center", behavior: "smooth" });
    }

	    function revealSession(sessionId, message = "") {
	      state.selectedSessionId = sessionId;
	      state.liveSessionId = "";
	      state.liveCursor = 0;
	      state.liveEvents = [];
	      renderSessionViews();
	      loadLiveEvents({ reset: true }).catch((error) => {
	        console.error(error);
	      });
	      requestAnimationFrame(() => scrollSessionIntoView(sessionId));
	      if (message) toast(message);
	    }

    function applyPendingFocus() {
      const sessionId = String(state.pendingFocusSessionId || "").trim();
      if (!sessionId) return;
      state.pendingFocusSessionId = "";
      const session = state.sessions.find((item) => item.id === sessionId);
      if (!session) {
        toast(`未找到会话: ${sessionId}`, true);
        return;
      }
      revealSession(sessionId, `已定位: ${sessionDisplayName(session)}`);
    }

    async function focusSessionById(sessionId) {
      const targetId = String(sessionId || "").trim();
      if (!targetId) return;
      const loaded = state.sessions.find((item) => item.id === targetId);
      if (loaded) {
        revealSession(targetId, `已定位: ${sessionDisplayName(loaded)}`);
        return;
      }
      state.pendingFocusSessionId = targetId;
      qEl.value = targetId;
      sourceFilterEl.value = "";
      archivedEl.checked = true;
      toast(`正在定位会话: ${targetId}`);
      await loadSessions();
    }

    function toggleSessionDetails(session) {
      state.expandedSessionId = state.expandedSessionId === session.id ? "" : session.id;
      renderSessionViews();
      if (state.expandedSessionId) {
        requestAnimationFrame(() => scrollSessionIntoView(session.id));
      }
    }

    function renderRows() {
      rowsEl.innerHTML = "";
      for (const session of state.sessions) {
        const tr = document.createElement("tr");
        tr.dataset.sessionId = session.id;

        const tdSelect = document.createElement("td");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = state.selected.has(session.id);
        checkbox.addEventListener("change", () => {
          setSessionSelected(session.id, checkbox.checked);
          renderSessionViews();
        });
        tdSelect.appendChild(checkbox);
        tr.appendChild(tdSelect);

        const tdTime = document.createElement("td");
        const timeStack = document.createElement("div");
        timeStack.className = "cell-stack";
        const timePrimary = document.createElement("div");
        timePrimary.className = "cell-title";
        timePrimary.textContent = sessionUpdatedText(session);
        timeStack.appendChild(timePrimary);
        const timeSecondary = document.createElement("div");
        timeSecondary.className = "cell-subtle mono";
        timeSecondary.textContent = `recorded: ${session.timestamp || "-"}`;
        timeStack.appendChild(timeSecondary);
        tdTime.appendChild(timeStack);
        tr.appendChild(tdTime);

        const tdId = document.createElement("td");
        const idStack = document.createElement("div");
        idStack.className = "cell-stack";
        const idTitle = document.createElement("div");
        idTitle.className = "cell-title";
        idTitle.textContent = session.alias || shortId(session.id);
        idStack.appendChild(idTitle);
        const idDiv = document.createElement("div");
        idDiv.className = "cell-subtle mono";
        idDiv.textContent = session.id;
        idStack.appendChild(idDiv);
        tdId.appendChild(idStack);
        tr.appendChild(tdId);

        const tdTitle = document.createElement("td");
        const titleStack = document.createElement("div");
        titleStack.className = "cell-stack";
        const titlePrimary = document.createElement("div");
        titlePrimary.className = "cell-title";
        const displayTitle = getDisplayTitle(session);
        titlePrimary.textContent = displayTitle;
        titleStack.appendChild(titlePrimary);
        const titleMeta = buildCompactTitleMeta(session);
        if (titleMeta) {
          titleStack.appendChild(titleMeta);
        }
        tdTitle.appendChild(titleStack);
        tr.appendChild(tdTitle);

        const tdSize = document.createElement("td");
        const sizeStack = document.createElement("div");
        sizeStack.className = "cell-stack";
        const sizePrimary = document.createElement("div");
        sizePrimary.className = "cell-title mono";
        sizePrimary.textContent = formatBytes(session.session_size_bytes);
        sizeStack.appendChild(sizePrimary);
        const sizeSecondary = document.createElement("div");
        sizeSecondary.className = "cell-subtle mono";
        sizeSecondary.textContent = `${session.session_size_bytes || 0} bytes`;
        sizeStack.appendChild(sizeSecondary);
        tdSize.appendChild(sizeStack);
        tr.appendChild(tdSize);

        const tdSource = document.createElement("td");
        const sourceStack = document.createElement("div");
        sourceStack.className = "cell-stack";
        const sourcePrimary = document.createElement("div");
        sourcePrimary.className = "cell-title";
        sourcePrimary.textContent = session.source_label || "-";
        sourceStack.appendChild(sourcePrimary);
        tdSource.appendChild(sourceStack);
        tr.appendChild(tdSource);

        const tdModel = document.createElement("td");
        const modelStack = document.createElement("div");
        modelStack.className = "cell-stack";
        const modelPrimary = document.createElement("div");
        modelPrimary.className = "cell-title";
        modelPrimary.textContent = sessionModelText(session);
        modelStack.appendChild(modelPrimary);
        tdModel.appendChild(modelStack);
        tr.appendChild(tdModel);

        const tdState = document.createElement("td");
        const stateRow = document.createElement("div");
        stateRow.className = "chip-row";
        stateRow.appendChild(chip(stateChipLabel(session), session.archived ? "warn" : "accent"));
        if (sessionRemoteRunning(session)) stateRow.appendChild(chip("网页续跑中", "warn"));
        if (session.alias) stateRow.appendChild(chip("有别名"));
        tdState.appendChild(stateRow);
        tr.appendChild(tdState);

        const tdCwd = document.createElement("td");
        tdCwd.className = "mono truncate-line";
        tdCwd.textContent = session.cwd_display || session.cwd || "-";
        tdCwd.title = session.cwd_raw || session.cwd || "-";
        tr.appendChild(tdCwd);

        const tdSlack = document.createElement("td");
        const slackValue = (session.slack_threads || []).join(", ") || "-";
        tdSlack.className = "mono truncate-line";
        tdSlack.textContent = slackValue;
        tdSlack.title = slackValue;
        tr.appendChild(tdSlack);

        const tdActions = document.createElement("td");
        tdActions.appendChild(buildCompactActionBar(session));
        tr.appendChild(tdActions);

        rowsEl.appendChild(tr);
        if (state.expandedSessionId === session.id) {
          const detailRow = document.createElement("tr");
          detailRow.className = "session-expand-row";
          detailRow.dataset.sessionId = session.id;
          const detailCell = document.createElement("td");
          detailCell.colSpan = 11;
          detailCell.appendChild(buildExpandedDetailPanel(session));
          detailRow.appendChild(detailCell);
          rowsEl.appendChild(detailRow);
        }
      }
    }

	    function renderCards() {
	      cardsEl.innerHTML = "";
	      if (!state.sessions.length) {
	        const empty = document.createElement("div");
	        empty.className = "empty-state";
	        empty.textContent = "没有匹配到 session。";
	        cardsEl.appendChild(empty);
	        return;
	      }

	      for (const session of state.sessions) {
	        const progress = session.progress || {};
	        const card = document.createElement("div");
	        card.className = `fleet-card${state.selectedSessionId === session.id ? " active" : ""}`;
	        card.dataset.sessionId = session.id;
	        card.addEventListener("click", () => selectSession(session.id));

	        const head = document.createElement("div");
	        head.className = "fleet-card-head";

	        const titleWrap = document.createElement("div");
	        const title = document.createElement("h2");
	        title.className = "fleet-card-title";
	        title.textContent = sessionDisplayName(session);
	        titleWrap.appendChild(title);

	        const subtitle = document.createElement("div");
	        subtitle.className = "fleet-card-subtitle";
	        subtitle.textContent = `${sessionStatusSummary(session)} | ${sessionModelText(session)}`;
	        titleWrap.appendChild(subtitle);
	        head.appendChild(titleWrap);

	        const updated = document.createElement("div");
	        updated.className = "fleet-card-updated mono";
	        updated.textContent = sessionUpdatedText(session);
	        head.appendChild(updated);

	        card.appendChild(head);
	        renderFleetMeta(session, card);

	        const preview = document.createElement("div");
	        preview.className = "fleet-card-preview";
	        preview.textContent = sessionPreviewText(session);
	        card.appendChild(preview);

	        const footer = document.createElement("div");
	        footer.className = "fleet-card-footer";
	        if (shouldShowRecentTools(session)) {
	          const tools = document.createElement("div");
	          tools.className = "fleet-card-tools";
	          tools.textContent = `最近工具: ${progress.recent_tools.join(", ")}`;
	          tools.title = tools.textContent;
	          footer.appendChild(tools);
	        }

	        const selectLabel = document.createElement("label");
	        selectLabel.className = "fleet-card-select";
	        selectLabel.addEventListener("click", (event) => event.stopPropagation());
	        const checkbox = document.createElement("input");
	        checkbox.type = "checkbox";
	        checkbox.checked = state.selected.has(session.id);
	        checkbox.addEventListener("change", () => {
	          setSessionSelected(session.id, checkbox.checked);
	          renderSessionViews();
	        });
	        selectLabel.appendChild(checkbox);
	        selectLabel.appendChild(document.createTextNode("批量"));
	        footer.appendChild(selectLabel);
	        card.appendChild(footer);

	        cardsEl.appendChild(card);
	      }
	    }

	    function renderSessionViews() {
	      if (state.remoteMarks.size) {
	        renderGuard(Array.from(state.remoteMarks.values()), state.remoteGuardWarning);
	      }
	      renderCards();
	      renderLivePanel();
	      updateSelectionUI();
	    }

    function formatBytes(value) {
      const bytes = Number(value || 0);
      if (!Number.isFinite(bytes) || bytes <= 0) return "0B";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let size = bytes;
      let unitIndex = 0;
      while (size >= 1024 && unitIndex < units.length - 1) {
        size /= 1024;
        unitIndex += 1;
      }
      if (unitIndex === 0) return `${Math.round(size)}${units[unitIndex]}`;
      if (size >= 100) return `${size.toFixed(0)}${units[unitIndex]}`;
      if (size >= 10) return `${size.toFixed(1)}${units[unitIndex]}`;
      return `${size.toFixed(2)}${units[unitIndex]}`;
    }

	    async function loadSessions() {
	      const query = encodeURIComponent(qEl.value.trim());
	      const sourceLabel = encodeURIComponent(sourceFilterEl.value || "");
	      const archived = archivedEl.checked ? "1" : "0";
	      const limit = encodeURIComponent(limitEl.value);
      const data = await api(`/api/sessions?q=${query}&source_label=${sourceLabel}&archived=${archived}&limit=${limit}`);
      state.sourceOptions = data.source_options || [];
      renderSourceOptions(state.sourceOptions);
      state.sessions = data.sessions || [];
	      if (state.expandedSessionId && !state.sessions.some((session) => session.id === state.expandedSessionId)) {
	        state.expandedSessionId = "";
	      }
	      reconcileSelection();
	      const previousSelectedId = state.selectedSessionId;
	      ensureSelectedSession();
	      renderSessionViews();
	      applyPendingFocus();
	      if (state.selectedSessionId) {
	        if (previousSelectedId !== state.selectedSessionId || state.liveSessionId !== state.selectedSessionId || !state.liveEvents.length) {
	          await loadLiveEvents({ reset: true });
	        }
	      } else {
	        stopLivePolling();
	      }
	      scheduleSessionRefresh();
	      metaEl.textContent = `机器: ${currentTargetLabel()} | 会话 ${data.count} 条 | 可见客户端 ${state.sourceOptions.length} 类 | 按最近文件变化排序`;
	    }

    async function loadGuard() {
      const data = await api("/api/remote_guard");
      renderGuard(data.sessions || [], data.warning || "");
    }

    async function refreshAll() {
      await bootstrapAuth();
      await loadTargets(true);
      if (maybeRequireCurrentTargetPassword()) return;
      await loadGuard();
      await loadSessions();
      toast(`已刷新：${state.sessions.length} 条`);
    }

    async function viewHistory(session) {
      try {
        toast(`加载最近 5 轮: ${session.id}`);
        const sessionKey = encodeURIComponent(session.id);
        const data = await api(`/api/history?session=${sessionKey}&rounds=5`);
        renderHistory(data.session, data.history || [], data.total || 0);
        toast(`历史已加载：${data.count} 条消息 / ${data.total} 轮`);
      } catch (error) {
        toast(error.message, true);
      }
    }

    async function setSessionTitle(session) {
      const initial = getSessionTitle(session) || getDisplayTitle(session);
      const title = prompt("输入新标题（最多140字符）", initial);
      if (title === null) return;
      const value = title.trim();
      if (!value) return toast("标题不能为空", true);
      try {
        await api("/api/set_title", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id, title: value })
        });
        await loadSessions();
      } catch (error) {
        toast(error.message, true);
      }
    }

    async function clearSessionTitle(session) {
      try {
        await api("/api/clear_title", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id })
        });
        await loadSessions();
      } catch (error) {
        toast(error.message, true);
      }
    }

    async function setSessionWorkdir(session) {
      const initial = session.cwd_raw || session.cwd || "";
      const cwd = prompt("输入新的工作目录路径", initial);
      if (cwd === null) return;
      const value = cwd.trim();
      if (!value) return toast("工作目录不能为空", true);
      try {
        await api("/api/set_workdir", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id, cwd: value })
        });
        await loadSessions();
      } catch (error) {
        toast(error.message, true);
      }
    }

    async function archiveSession(session) {
      if (!confirm(`归档会话 ${session.id} ?`)) return;
      try {
        await api("/api/archive", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id })
        });
        await loadSessions();
      } catch (error) {
        toast(error.message, true);
      }
    }

    async function deleteSession(session) {
      if (!confirm(`永久删除会话 ${session.id} ? 此操作不可恢复。`)) return;
      try {
        await api("/api/delete", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id, confirm: "DELETE" })
        });
        await loadSessions();
      } catch (error) {
        toast(error.message, true);
      }
    }

    async function copyResumeCmd(session) {
      try {
        const data = await api("/api/resume_cmd", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id })
        });
        await navigator.clipboard.writeText(data.command);
        toast(`已复制终端恢复命令（未执行）: ${data.command}`);
      } catch (error) {
        toast(error.message, true);
      }
    }

    async function continueSession(session) {
      try {
        const data = await api("/api/continue", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id, prompt: DEFAULT_CONTINUE_PROMPT })
        });
        toast(`已发出：${displayContinuePrompt(data.prompt_display || data.prompt)}。在 /remote 或 Guard 面板回到“等你继续”前，不要从 VS Code 再发消息。`);
        await refreshAll();
      } catch (error) {
        toast(error.message, true);
      }
    }

	    async function customContinueSession(session, providedText = null) {
	      let input = providedText;
	      if (input === null) {
	        input = window.prompt("发给该会话的消息", "");
	      }
	      if (input === null) return;
	      const value = String(input || "").trim();
	      if (!value) return toast("消息不能为空", true);
	      try {
	        const data = await api("/api/continue", {
	          method: "POST",
	          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id, prompt: value })
	        });
	        toast(`已发出：${displayContinuePrompt(data.prompt_display || data.prompt)}。`);
	        if (state.selectedSessionId === session.id) {
	          liveComposerInputEl.value = "";
	        }
	        await refreshAll();
	      } catch (error) {
	        toast(error.message, true);
	      }
	    }

    async function stopContinue(session) {
      if (!confirm(`停止该会话当前由网页触发的运行？\n${session.id}`)) return;
      try {
        const data = await api("/api/stop", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session: session.id })
        });
        toast(`已停止：${data.session_id}`);
        await refreshAll();
      } catch (error) {
        toast(error.message, true);
      }
    }

    async function loadStats() {
      const query = encodeURIComponent(qEl.value.trim());
      const sourceLabel = encodeURIComponent(sourceFilterEl.value || "");
      const archived = archivedEl.checked ? "1" : "0";
      try {
        const data = await api(`/api/stats?q=${query}&source_label=${sourceLabel}&archived=${archived}`);
        const stats = data.stats;
        toast(
          `总数=${stats.total}，活动=${stats.active}，归档=${stats.archived}，` +
          `别名=${stats.with_alias}，Slack=${stats.with_slack_thread}`
        );
      } catch (error) {
        toast(error.message, true);
      }
    }

    async function batchArchiveSelected() {
      const ids = state.sessions.filter((session) => state.selected.has(session.id)).map((session) => session.id);
      if (!ids.length) return toast("请先选择要归档的会话", true);
      if (!confirm(`归档已选 ${ids.length} 条会话？`)) return;
      try {
        const data = await api("/api/batch_archive", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session_ids: ids })
        });
        state.selected.clear();
        await loadSessions();
        toast(`批量归档完成: archived=${data.archived}, already=${data.already_archived}, not_found=${data.not_found}, conflict=${data.conflict}`);
      } catch (error) {
        toast(error.message, true);
      }
    }

    async function batchDeleteSelected() {
      const ids = state.sessions.filter((session) => state.selected.has(session.id)).map((session) => session.id);
      if (!ids.length) return toast("请先选择要删除的会话", true);
      const confirmText = prompt(`即将永久删除 ${ids.length} 条会话，输入 DELETE 确认`);
      if (confirmText !== "DELETE") return toast("已取消批量删除");
      try {
        const data = await api("/api/batch_delete", {
          method: "POST",
          headers: jsonHeaders(),
          body: JSON.stringify({ session_ids: ids, confirm: "DELETE" })
        });
        state.selected.clear();
        await loadSessions();
        toast(`批量删除完成: deleted=${data.deleted}, not_found=${data.not_found}, missing_file=${data.missing_file}`);
      } catch (error) {
        toast(error.message, true);
      }
    }

	    document.getElementById("refresh").addEventListener("click", () => refreshAll().catch((e) => toast(e.message, true)));
	    targetSelectEl.addEventListener("change", () => {
	      currentTargetId = targetSelectEl.value || LOCAL_TARGET_ID;
	      saveTarget(currentTargetId);
	      stopLivePolling();
	      stopSessionRefresh();
	      state.liveEvents = [];
	      state.liveCursor = 0;
	      state.liveSessionId = "";
	      if (maybeRequireCurrentTargetPassword()) return;
	      refreshAll().catch((e) => toast(e.message, true));
	    });
    addTargetBtnEl.addEventListener("click", () => {
      openTargetEditor(currentTargetId !== LOCAL_TARGET_ID ? currentTarget() : null);
    });
    document.getElementById("remotePageBtn").addEventListener("click", () => {
      window.location.href = "/remote";
    });
    document.getElementById("logoutBtn").addEventListener("click", async () => {
      try {
        await api("/api/logout", { method: "POST", headers: jsonHeaders() });
      } catch (error) {
        toast(error.message, true);
      } finally {
        window.location.href = "/login";
      }
    });
    document.getElementById("statsBtn").addEventListener("click", () => loadStats().catch((e) => toast(e.message, true)));
    document.getElementById("selectVisible").addEventListener("click", () => {
      for (const session of state.sessions) state.selected.add(session.id);
      renderSessionViews();
      toast(`已选择当前页 ${state.sessions.length} 条`);
    });
    document.getElementById("clearSelection").addEventListener("click", () => {
      state.selected.clear();
      renderSessionViews();
      toast("已清空选择");
    });
    batchArchiveBtnEl.addEventListener("click", () => batchArchiveSelected().catch((e) => toast(e.message, true)));
	    batchDeleteBtnEl.addEventListener("click", () => batchDeleteSelected().catch((e) => toast(e.message, true)));
	    liveSendBtnEl.addEventListener("click", () => {
	      const session = selectedSession();
	      if (!session) return toast("请先选择一个 session", true);
	      customContinueSession(session, liveComposerInputEl.value).catch((e) => toast(e.message, true));
	    });
	    liveContinueBtnEl.addEventListener("click", () => {
	      const session = selectedSession();
	      if (!session) return toast("请先选择一个 session", true);
	      continueSession(session).catch((e) => toast(e.message, true));
	    });
	    liveStopBtnEl.addEventListener("click", () => {
	      const session = selectedSession();
	      if (!session) return toast("请先选择一个 session", true);
	      stopContinue(session).catch((e) => toast(e.message, true));
	    });
	    liveRefreshBtnEl.addEventListener("click", () => {
	      loadLiveEvents({ reset: true }).catch((e) => toast(e.message, true));
	    });
	    liveHistoryBtnEl.addEventListener("click", () => {
	      const session = selectedSession();
	      if (!session) return toast("请先选择一个 session", true);
	      viewHistory(session).catch((e) => toast(e.message, true));
	    });
	    liveResumeCmdBtnEl.addEventListener("click", () => {
	      const session = selectedSession();
	      if (!session) return toast("请先选择一个 session", true);
	      copyResumeCmd(session).catch((e) => toast(e.message, true));
	    });
	    liveSetTitleBtnEl.addEventListener("click", () => {
	      const session = selectedSession();
	      if (!session) return toast("请先选择一个 session", true);
	      setSessionTitle(session).catch((e) => toast(e.message, true));
	    });
	    liveSetSourceBtnEl.addEventListener("click", () => {
	      const session = selectedSession();
	      if (!session) return toast("请先选择一个 session", true);
	      openSourceEditor(session);
	    });
	    liveSetWorkdirBtnEl.addEventListener("click", () => {
	      const session = selectedSession();
	      if (!session) return toast("请先选择一个 session", true);
	      setSessionWorkdir(session).catch((e) => toast(e.message, true));
	    });
	    selectAllEl.addEventListener("change", () => {
	      if (selectAllEl.checked) {
	        for (const session of state.sessions) state.selected.add(session.id);
	      } else {
        for (const session of state.sessions) state.selected.delete(session.id);
      }
      renderSessionViews();
    });
    historyCloseEl.addEventListener("click", closeHistory);
    historyCloseBottomEl.addEventListener("click", closeHistory);
    historyBackdropEl.addEventListener("click", closeHistory);
    sourceCloseEl.addEventListener("click", closeSourceEditor);
    sourceCancelEl.addEventListener("click", closeSourceEditor);
    sourceBackdropEl.addEventListener("click", closeSourceEditor);
    sourceSaveEl.addEventListener("click", () => saveSourceEditor().catch((e) => toast(e.message, true)));
    targetCloseEl.addEventListener("click", closeTargetEditor);
    targetCancelEl.addEventListener("click", closeTargetEditor);
    targetDeleteEl.addEventListener("click", () => deleteTargetEditor().then(() => refreshAll()).catch((e) => {
      targetMetaEl.textContent = e.message || String(e);
      toast(e.message || String(e), true);
    }));
    targetBackdropEl.addEventListener("click", closeTargetEditor);
    targetAuthModeEl.addEventListener("change", syncTargetPasswordField);
    targetCheckEl.addEventListener("click", () => checkTargetEditor().catch((e) => {
      targetMetaEl.textContent = e.message || String(e);
      toast(e.message || String(e), true);
    }));
    targetBootstrapEl.addEventListener("click", () => bootstrapTargetEditor().then(() => refreshAll()).catch((e) => {
      targetMetaEl.textContent = e.message || String(e);
      toast(e.message || String(e), true);
    }));
    targetSaveEl.addEventListener("click", () => saveTargetEditor().then(() => refreshAll()).catch((e) => {
      targetMetaEl.textContent = e.message || String(e);
      toast(e.message || String(e), true);
    }));
    sourceCustomInputEl.addEventListener("input", () => {
      state.sourceEditor.custom = sourceCustomInputEl.value;
      renderSourceEditor();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (targetPanelEl.style.display === "block") {
        closeTargetEditor();
        return;
      }
      if (sourcePanelEl.style.display === "block") {
        closeSourceEditor();
        return;
      }
      if (historyPanelEl.style.display === "block") closeHistory();
    });
	    qEl.addEventListener("keydown", (event) => {
	      if (event.key === "Enter") refreshAll().catch((e) => toast(e.message, true));
	    });
	    liveComposerInputEl.addEventListener("keydown", (event) => {
	      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
	        event.preventDefault();
	        liveSendBtnEl.click();
	      }
	    });
	    liveEventsEl.addEventListener("scroll", () => {
	      state.liveStickToBottom = isNearBottom(liveEventsEl);
	    });
	    sourceFilterEl.addEventListener("change", () => refreshAll().catch((e) => toast(e.message, true)));
	    archivedEl.addEventListener("change", () => refreshAll().catch((e) => toast(e.message, true)));
	    limitEl.addEventListener("change", () => refreshAll().catch((e) => toast(e.message, true)));
	    document.addEventListener("visibilitychange", () => {
	      if (document.hidden) {
	        stopLivePolling();
	        stopSessionRefresh();
	        return;
	      }
	      scheduleLivePolling();
	      scheduleSessionRefresh();
	    });
	    currentTargetId = loadSavedTarget();
	    refreshAll().catch((e) => toast(e.message, true));
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    default_codex_home = Path.home() / ".codex"
    default_slack_db = repo_root / "sessions.json"
    parser = argparse.ArgumentParser(description="Web UI for local Codex session management")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-home", type=Path, default=default_codex_home)
    parser.add_argument("--slack-db", type=Path, default=default_slack_db)
    parser.add_argument(
        "--aliases-db",
        "--overrides-db",
        dest="aliases_db",
        type=Path,
        default=default_codex_home / "session_aliases.json",
        help="Session override DB path (legacy default filename: ~/.codex/session_aliases.json)",
    )
    parser.add_argument("--auth-file", type=Path, default=default_auth_file())
    parser.add_argument("--targets-file", type=Path, default=default_targets_file())
    parser.add_argument(
        "--set-password-stdin",
        action="store_true",
        help="Read a password from stdin, write auth config, and exit",
    )
    parser.add_argument(
        "--set-password-prompt",
        action="store_true",
        help="Prompt for a password, write auth config, and exit",
    )
    parser.add_argument(
        "--set-random-password",
        action="store_true",
        help="Generate a random password, write auth config, print it, and exit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    auth_file = args.auth_file.expanduser().resolve()
    password_setup_flags = [args.set_password_stdin, args.set_password_prompt, args.set_random_password]
    if sum(1 for enabled in password_setup_flags if enabled) > 1:
        raise SystemExit("Choose only one of --set-password-stdin / --set-password-prompt / --set-random-password")

    if args.set_password_stdin:
        password = sys.stdin.read().rstrip("\r\n")
        validate_password(password)
        path = write_auth_config(auth_file, password)
        print(f"Wrote auth config to {display_path(path)}")
        return 0

    if args.set_password_prompt:
        password = getpass.getpass("New password: ")
        validate_password(password)
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise SystemExit("Password confirmation mismatch")
        path = write_auth_config(auth_file, password)
        print(f"Wrote auth config to {display_path(path)}")
        return 0

    if args.set_random_password:
        password = generate_password()
        path = write_auth_config(auth_file, password)
        print(f"Wrote auth config to {display_path(path)}")
        print(password)
        return 0

    codex_home = args.codex_home.expanduser().resolve()
    slack_db: Optional[Path] = args.slack_db.expanduser().resolve()
    if not slack_db.exists():
        slack_db = None
    aliases_db = args.aliases_db.expanduser().resolve()
    targets_path = args.targets_file.expanduser().resolve()
    remote_marks_path = codex_home / "web_remote_marks.json"
    remote_watchlist_path = codex_home / "web_remote_watchlist.json"
    supervisor_lock_path = codex_home / "web_auto_continue.lock"
    auth = load_auth_config(auth_file)
    context = AppContext(
        codex_home=codex_home,
        slack_db=slack_db,
        aliases_db=aliases_db,
        codex_bin=args.codex_bin,
        auth=auth,
        targets_path=targets_path,
        remote_marks_path=remote_marks_path,
        remote_watchlist_path=remote_watchlist_path,
        supervisor_lock_path=supervisor_lock_path,
        lock=threading.Lock(),
        targets=load_machine_targets(targets_path),
        resume_jobs={},
        auth_failures={},
        remote_marks=load_remote_marks(remote_marks_path),
        remote_watchlist=load_remote_watchlist(remote_watchlist_path),
        shutdown_event=threading.Event(),
    )
    supervisor = threading.Thread(
        target=auto_continue_loop,
        args=(context,),
        name="codex-auto-continue",
        daemon=True,
    )
    supervisor.start()
    server = SessionServer((args.host, args.port), context)
    print(f"Serving Codex Sessions UI at http://{args.host}:{args.port}")
    print(f"Auth: {'enabled' if auth else 'disabled'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        context.shutdown_event.set()
        supervisor.join(timeout=1.0)
        for job in list(context.resume_jobs.values()):
            try:
                job.log_handle.close()
            except Exception:
                pass
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
