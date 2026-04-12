#!/usr/bin/env python3
from __future__ import annotations

import ast
import argparse
import json
import os
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


OVERRIDE_FIELDS = {"alias", "title", "cwd"}
TITLE_NOISE_MARKERS = (
    "Invalid JSON?",
    "# Context from my IDE setup",
    "## My request for Codex:",
    "<environment_context>",
    "Please output JSON fields",
    "请输出JSON字段",
    "Wait! title has",
    "However contains quote",
    "contains quote",
)
SOURCE_PRESETS: Dict[str, Dict[str, str]] = {
    "vscode": {
        "label": "VS Code",
        "long_label": "VS Code 扩展",
        "description": "优先让线程出现在 VS Code 会话列表。",
    },
    "cli": {
        "label": "Codex CLI",
        "long_label": "Codex CLI",
        "description": "优先给官方 Codex CLI 的 resume picker。",
    },
    "exec": {
        "label": "Exec",
        "long_label": "Exec / 非交互",
        "description": "保留 exec 风格的非交互线程。",
    },
    "subagent": {
        "label": "子 Agent",
        "long_label": "子 Agent / 内部",
        "description": "内部派生线程，不适合作为主会话入口。",
    },
    "json": {
        "label": "内部 JSON",
        "long_label": "内部 JSON source",
        "description": "看起来像内部状态串，不建议直接拿来做客户端可见性。",
    },
    "unknown": {
        "label": "未标记",
        "long_label": "未标记",
        "description": "缺少明确 source，客户端可见性不稳定。",
    },
}
SOURCE_ALIASES = {
    "vs code": "vscode",
    "vs-code": "vscode",
    "vscode": "vscode",
    "editor": "vscode",
    "ide": "vscode",
    "cli": "cli",
    "terminal": "cli",
    "codex cli": "cli",
    "codex-cli": "cli",
    "exec": "exec",
}
SOURCE_DISPLAY_ORDER = {
    "vscode": 0,
    "cli": 1,
    "exec": 2,
    "subagent": 3,
    "json": 4,
    "unknown": 5,
    "custom": 6,
}


def build_official_title_sync_result(
    *,
    attempted: bool,
    status: str,
    error: str = "",
    targets: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    normalized_error = str(error or "").strip()
    result: Dict[str, Any] = {
        "official_title_sync_attempted": attempted,
        "official_title_sync": status,
        "official_title_sync_error": normalized_error,
        # Keep legacy keys for callers that still expect the old name.
        "thread_name_sync_attempted": attempted,
        "thread_name_sync": status,
        "thread_name_sync_error": normalized_error,
    }
    if targets is not None:
        result["official_title_sync_targets"] = targets
        result["thread_name_sync_targets"] = targets
    return result


@dataclass
class SessionRecord:
    session_id: str
    timestamp: str
    cwd: str
    originator: str
    source: str
    session_source: str
    client_source: str
    cli_version: str
    model_provider: str
    model: str
    effort: str
    title: str
    thread_name: str
    vscode_display_name: str
    path: Path
    session_size_bytes: int
    archived: bool
    slack_threads: List[str]
    alias: str
    parent_session_id: str
    parent_display_title: str
    subagent_nickname: str
    subagent_role: str
    subagent_depth: int


def iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def display_path(path_value: Path | str) -> str:
    text = str(path_value or "").strip()
    if not text:
        return ""
    try:
        path = Path(text).expanduser()
    except Exception:
        return text
    try:
        resolved = path.resolve(strict=False)
    except Exception:
        resolved = path
    home = Path.home().resolve()
    try:
        relative = resolved.relative_to(home)
    except ValueError:
        return str(resolved)
    if not relative.parts:
        return "~"
    return f"~/{relative.as_posix()}"


def compress_space(text: str) -> str:
    return " ".join((text or "").split())


def normalize_title_candidate(text: str, limit: int = 140) -> str:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    if not lines:
        return ""
    candidate = compress_space(lines[0])
    if len(candidate) > limit:
        candidate = candidate[: limit - 1] + "…"
    return candidate


def title_looks_noisy(text: str) -> bool:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return True
    raw_lower = raw.lower()
    if any(marker.lower() in raw_lower for marker in TITLE_NOISE_MARKERS):
        return True
    if "](" in raw or raw.startswith("["):
        return True
    if "**" in raw or "`" in raw:
        return True
    if raw.endswith("}") and "{" not in raw:
        return True
    if raw.lstrip().startswith("{") and raw.rstrip().endswith("}"):
        return True
    if raw.count("\n") >= 2:
        return True
    candidate = normalize_title_candidate(raw, limit=180)
    if not candidate:
        return True
    if len(candidate) > 160:
        return True
    if candidate.lower().startswith("you are ") and len(candidate) > 80:
        return True
    return False


def choose_display_title(*candidates: str) -> str:
    fallback = ""
    for text in candidates:
        candidate = normalize_title_candidate(text)
        if not candidate:
            continue
        if not fallback:
            fallback = candidate
        if not title_looks_noisy(text):
            return candidate
    return fallback


def session_record_display_name(record: SessionRecord) -> str:
    return (
        choose_display_title(
            record.alias,
            record.vscode_display_name,
            record.thread_name,
            record.title,
            record.session_id,
        )
        or record.session_id
    )


def infer_source_from_originator(originator: str) -> str:
    raw = str(originator or "").strip().lower()
    if not raw:
        return ""
    if "vscode" in raw:
        return "vscode"
    if raw.endswith("_cli_rs") or raw.endswith("_cli") or "cli" in raw:
        return "cli"
    if "exec" in raw:
        return "exec"
    return ""


def normalize_source_value(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered in SOURCE_ALIASES:
        return SOURCE_ALIASES[lowered]
    if raw.startswith("{") and '"subagent"' in raw:
        return "subagent"
    if raw.startswith("{") and raw.endswith("}"):
        return "json"
    return lowered


def describe_source(originator: str, source: str) -> Dict[str, str]:
    raw_source = str(source or "").strip()
    inferred = normalize_source_value(raw_source) or infer_source_from_originator(originator)
    if inferred in SOURCE_PRESETS:
        preset = SOURCE_PRESETS[inferred]
        return {
            "kind": inferred,
            "filter_key": inferred,
            "label": preset["label"],
            "long_label": preset["long_label"],
            "description": preset["description"],
            "raw_source": raw_source,
            "originator": str(originator or "").strip(),
        }
    if not raw_source:
        preset = SOURCE_PRESETS["unknown"]
        return {
            "kind": "unknown",
            "filter_key": "unknown",
            "label": preset["label"],
            "long_label": preset["long_label"],
            "description": preset["description"],
            "raw_source": "",
            "originator": str(originator or "").strip(),
        }
    clipped = normalize_title_candidate(raw_source, limit=44)
    return {
        "kind": "custom",
        "filter_key": raw_source.lower(),
        "label": clipped,
        "long_label": f"自定义: {clipped}",
        "description": "自定义 source；官方客户端是否识别，取决于它们自己的可见性逻辑。",
        "raw_source": raw_source,
        "originator": str(originator or "").strip(),
    }


def build_source_search_blob(originator: str, source: str) -> str:
    info = describe_source(originator, source)
    return " ".join(
        part
        for part in [
            str(originator or "").strip(),
            str(source or "").strip(),
            info.get("kind", ""),
            info.get("filter_key", ""),
            info.get("label", ""),
            info.get("long_label", ""),
            info.get("description", ""),
        ]
        if part
    )


def source_sort_key(source_key: str, label: str = "") -> tuple[int, str]:
    normalized = str(source_key or "").strip().lower()
    order = SOURCE_DISPLAY_ORDER.get(normalized, SOURCE_DISPLAY_ORDER["custom"])
    return (order, str(label or normalized))


def parse_structured_source(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    for parser in (json.loads, ast.literal_eval):
        try:
            value = parser(text)
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return {}


def extract_subagent_relationship(*source_values: str) -> Dict[str, Any]:
    for raw in source_values:
        payload = parse_structured_source(raw)
        subagent = payload.get("subagent")
        if not isinstance(subagent, dict):
            continue
        spawn = subagent.get("thread_spawn")
        if not isinstance(spawn, dict):
            continue
        parent_session_id = str(spawn.get("parent_thread_id") or "").strip()
        if not parent_session_id:
            continue
        depth_value = spawn.get("depth")
        try:
            depth = int(depth_value)
        except Exception:
            depth = 0
        return {
            "parent_session_id": parent_session_id,
            "subagent_nickname": str(spawn.get("agent_nickname") or "").strip(),
            "subagent_role": str(spawn.get("agent_role") or "").strip(),
            "subagent_depth": depth,
        }
    return {
        "parent_session_id": "",
        "subagent_nickname": "",
        "subagent_role": "",
        "subagent_depth": 0,
    }


def derive_title_from_text(text: str) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""

    marker = "## My request for Codex:"
    if marker in raw:
        raw = raw.split(marker, 1)[1].strip()
    elif "My request for Codex:" in raw:
        raw = raw.split("My request for Codex:", 1)[1].strip()

    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    if not lines:
        return ""

    first_line = lines[0]
    if first_line.startswith("# AGENTS.md instructions"):
        return ""
    if first_line.startswith("<environment_context>"):
        return ""
    if first_line.startswith("# Context from my IDE setup"):
        return ""

    first_line = first_line.lstrip("#").strip()
    return normalize_title_candidate(first_line)


def extract_user_text_from_obj(obj: Dict[str, Any]) -> str:
    obj_type = obj.get("type")
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        return ""

    if obj_type == "event_msg" and payload.get("type") == "user_message":
        return str(payload.get("message") or "")

    if obj_type == "response_item":
        if payload.get("type") != "message" or payload.get("role") != "user":
            return ""
        chunks = payload.get("content")
        if not isinstance(chunks, list):
            return ""
        texts = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") in {"input_text", "output_text"}:
                value = str(chunk.get("text") or "").strip()
                if value:
                    texts.append(value)
        return "\n".join(texts)

    return ""


def rewrite_user_prompt_text(raw_text: str, title: str) -> str:
    normalized_title = str(title or "").strip()
    if not normalized_title:
        return raw_text

    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
    markers = ("## My request for Codex:", "My request for Codex:")
    for marker in markers:
        marker_index = text.find(marker)
        if marker_index < 0:
            continue
        prefix = text[: marker_index + len(marker)]
        return f"{prefix}\n{normalized_title}\n"
    return normalized_title


def read_jsonl_meta(path: Path) -> Optional[SessionRecord]:
    try:
        session_size_bytes = path.stat().st_size
        with path.open("r", encoding="utf-8") as file_obj:
            first = file_obj.readline().strip()
            if not first:
                return None
            first_obj = json.loads(first)
            if first_obj.get("type") != "session_meta":
                return None
            payload = first_obj.get("payload", {})

            model = ""
            effort = ""
            title = str(payload.get("title") or "").strip()
            for _ in range(1200):
                line = file_obj.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                user_text = extract_user_text_from_obj(obj)
                if user_text and not title:
                    candidate = derive_title_from_text(user_text)
                    if candidate:
                        title = candidate
                if obj.get("type") == "turn_context":
                    context = obj.get("payload", {})
                    model = str(context.get("model") or "")
                    effort = str(context.get("effort") or "")
                if model and effort and title:
                    break

            session_id = str(payload.get("id") or "")
            if not session_id:
                return None

            return SessionRecord(
                session_id=session_id,
                timestamp=str(payload.get("timestamp") or ""),
                cwd=str(payload.get("cwd") or ""),
                originator=str(payload.get("originator") or ""),
                source=str(payload.get("source") or ""),
                session_source=str(payload.get("source") or ""),
                client_source="",
                cli_version=str(payload.get("cli_version") or ""),
                model_provider=str(payload.get("model_provider") or ""),
                model=model,
                effort=effort,
                title=title,
                thread_name="",
                vscode_display_name="",
                path=path,
                session_size_bytes=session_size_bytes,
                archived=("archived_sessions" in path.parts),
                slack_threads=[],
                alias="",
                parent_session_id="",
                parent_display_title="",
                subagent_nickname="",
                subagent_role="",
                subagent_depth=0,
            )
    except Exception:
        return None


def update_session_metadata(
    path: Path,
    *,
    title: Optional[str] = None,
    cwd: Optional[str] = None,
    source: Optional[str] = None,
) -> Dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    updated_lines: List[str] = []
    previous = {"title": "", "cwd": "", "source": ""}
    saw_session_meta = False
    changed = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            updated_lines.append(raw_line)
            continue

        try:
            obj = json.loads(line)
        except Exception:
            updated_lines.append(raw_line)
            continue
        if not isinstance(obj, dict):
            updated_lines.append(raw_line)
            continue

        line_changed = False
        obj_type = obj.get("type")
        payload = obj.get("payload")
        if isinstance(payload, dict):
            if obj_type == "session_meta":
                saw_session_meta = True
                previous["title"] = str(payload.get("title") or "").strip()
                previous["cwd"] = str(payload.get("cwd") or "").strip()
                previous["source"] = str(payload.get("source") or "").strip()
                if title is not None:
                    normalized_title = str(title).strip()
                    if normalized_title:
                        if str(payload.get("title") or "").strip() != normalized_title:
                            payload["title"] = normalized_title
                            line_changed = True
                    else:
                        if "title" in payload:
                            payload.pop("title", None)
                            line_changed = True
                if cwd is not None:
                    normalized_cwd = str(cwd).strip()
                    if str(payload.get("cwd") or "").strip() != normalized_cwd:
                        payload["cwd"] = normalized_cwd
                        line_changed = True
                if source is not None:
                    normalized_source = str(source).strip()
                    if str(payload.get("source") or "").strip() != normalized_source:
                        payload["source"] = normalized_source
                        line_changed = True
            elif obj_type == "turn_context" and cwd is not None:
                normalized_cwd = str(cwd).strip()
                if str(payload.get("cwd") or "").strip() != normalized_cwd:
                    payload["cwd"] = normalized_cwd
                    line_changed = True

        if line_changed:
            changed = True
            updated_lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        else:
            updated_lines.append(raw_line)

    if not saw_session_meta:
        raise ValueError(f"Invalid session file (missing session_meta): {display_path(path)}")

    if changed:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    return previous


def update_first_user_preview(path: Path, title: str) -> Dict[str, Any]:
    normalized_title = str(title or "").strip()
    if not normalized_title:
        return {"preview_changed": False, "previous_preview": ""}

    lines = path.read_text(encoding="utf-8").splitlines()
    updated_lines: List[str] = []
    changed = False
    updated_response_item = False
    updated_event_msg = False
    previous_preview = ""

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            updated_lines.append(raw_line)
            continue

        try:
            obj = json.loads(line)
        except Exception:
            updated_lines.append(raw_line)
            continue
        if not isinstance(obj, dict):
            updated_lines.append(raw_line)
            continue

        line_changed = False
        obj_type = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            updated_lines.append(raw_line)
            continue

        if (
            not updated_response_item
            and obj_type == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            content = payload.get("content")
            if isinstance(content, list):
                for chunk in content:
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("type") not in {"input_text", "output_text", "text"}:
                        continue
                    original_text = str(chunk.get("text") or "")
                    if not previous_preview:
                        previous_preview = derive_title_from_text(original_text)
                    updated_text = rewrite_user_prompt_text(original_text, normalized_title)
                    if original_text != updated_text:
                        chunk["text"] = updated_text
                        line_changed = True
                    updated_response_item = True
                    break

        if not updated_event_msg and obj_type == "event_msg" and payload.get("type") == "user_message":
            original_message = str(payload.get("message") or "")
            if original_message and not previous_preview:
                previous_preview = derive_title_from_text(original_message)
            updated_message = rewrite_user_prompt_text(original_message, normalized_title)
            if original_message != updated_message:
                payload["message"] = updated_message
                line_changed = True
            updated_event_msg = True

        if line_changed:
            changed = True
            updated_lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        else:
            updated_lines.append(raw_line)

    if changed:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
        tmp_path.replace(path)

    return {
        "preview_changed": changed,
        "previous_preview": previous_preview,
    }


def iter_session_files(codex_home: Path, include_archived: bool) -> Iterable[Path]:
    sessions_root = codex_home / "sessions"
    if sessions_root.exists():
        for path in sessions_root.rglob("*.jsonl"):
            if path.is_file():
                yield path

    if include_archived:
        archived_root = codex_home / "archived_sessions"
        if archived_root.exists():
            for path in archived_root.glob("*.jsonl"):
                if path.is_file():
                    yield path


def load_thread_state_index(codex_home: Path) -> Dict[str, Dict[str, Any]]:
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        return {}

    mapping: Dict[str, Dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except Exception:
        return {}

    try:
        rows = conn.execute("SELECT id, source, title, cwd, updated_at FROM threads")
        for row in rows:
            session_id = str(row["id"] or "").strip()
            if not session_id:
                continue
            mapping[session_id] = {
                "source": str(row["source"] or "").strip(),
                "title": str(row["title"] or "").strip(),
                "cwd": str(row["cwd"] or "").strip(),
                "updated_at": int(row["updated_at"] or 0),
            }
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return mapping


def load_thread_name_index(codex_home: Path) -> Dict[str, str]:
    index_path = codex_home / "session_index.jsonl"
    if not index_path.exists():
        return {}

    mapping: Dict[str, str] = {}
    try:
        with index_path.open("r", encoding="utf-8") as file_obj:
            for raw_line in file_obj:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                session_id = str(payload.get("id") or payload.get("thread_id") or "").strip()
                if not session_id:
                    continue
                thread_name = str(payload.get("thread_name") or "").strip()
                if thread_name:
                    mapping[session_id] = thread_name
    except Exception:
        return {}

    return mapping


def append_session_index_entry(codex_home: Path, session_id: str, thread_name: str) -> bool:
    normalized_session_id = str(session_id or "").strip()
    normalized_thread_name = str(thread_name or "").strip()
    if not normalized_session_id or not normalized_thread_name:
        return False

    index_path = codex_home / "session_index.jsonl"
    payload = {
        "id": normalized_session_id,
        "thread_name": normalized_thread_name,
        "updated_at": iso_now(),
    }
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with index_path.open("a", encoding="utf-8") as file_obj:
            file_obj.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        return False
    return True


def load_vscode_task_title_index(codex_home: Path) -> Dict[str, str]:
    index_path = codex_home / "vscode_task_list.json"
    if not index_path.exists():
        return {}

    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    mapping: Dict[str, str] = {}
    entries = payload if isinstance(payload, list) else []
    for item in entries:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("conversationId") or item.get("id") or "").strip()
        if not session_id:
            continue
        preview = str(item.get("preview") or item.get("label") or "").strip()
        if preview:
            mapping[session_id] = preview
    return mapping


def load_slack_index(slack_db: Optional[Path]) -> Dict[str, List[str]]:
    if not slack_db:
        return {}
    if not slack_db.exists():
        return {}
    try:
        content = json.loads(slack_db.read_text(encoding="utf-8"))
    except Exception:
        return {}

    mapping: Dict[str, List[str]] = {}
    for thread_key, session in content.items():
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("last_session_id") or "").strip()
        if not session_id:
            continue
        mapping.setdefault(session_id, []).append(str(thread_key))
    return mapping


def normalize_override_entry(raw_value: Any) -> Dict[str, str]:
    entry: Dict[str, str] = {}
    if isinstance(raw_value, str):
        alias = raw_value.strip()
        if alias:
            entry["alias"] = alias
        return entry
    if not isinstance(raw_value, dict):
        return entry
    alias = str(raw_value.get("alias") or "").strip()
    if alias:
        entry["alias"] = alias
    title = str(raw_value.get("title") or "").strip()
    if title:
        entry["title"] = title
    cwd_value = raw_value.get("cwd")
    if cwd_value is None:
        cwd_value = raw_value.get("workdir")
    cwd = str(cwd_value or "").strip()
    if cwd:
        entry["cwd"] = cwd
    return entry


def load_overrides(overrides_db: Optional[Path]) -> Dict[str, Dict[str, str]]:
    if not overrides_db:
        return {}
    if not overrides_db.exists():
        return {}
    try:
        payload = json.loads(overrides_db.read_text(encoding="utf-8"))
    except Exception:
        return {}

    overrides: Dict[str, Dict[str, str]] = {}
    if not isinstance(payload, dict):
        return overrides

    for session_id, raw_value in payload.items():
        sid = str(session_id).strip()
        if not sid:
            continue
        entry = normalize_override_entry(raw_value)
        if entry:
            overrides[sid] = entry
    return overrides


def save_overrides(overrides_db: Path, overrides: Dict[str, Dict[str, str]]) -> None:
    overrides_db.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {}
    for session_id in sorted(overrides):
        sid = str(session_id).strip()
        if not sid:
            continue
        entry = normalize_override_entry(overrides.get(session_id, {}))
        if not entry:
            continue
        if set(entry.keys()) == {"alias"}:
            payload[sid] = entry["alias"]
        else:
            payload[sid] = entry
    overrides_db.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_aliases(aliases_db: Optional[Path]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    overrides = load_overrides(aliases_db)
    for session_id, entry in overrides.items():
        label = str(entry.get("alias") or "").strip()
        if label:
            aliases[session_id] = label
    return aliases


def save_aliases(aliases_db: Path, aliases: Dict[str, str]) -> None:
    overrides = load_overrides(aliases_db)
    desired_aliases = {key: value for key, value in aliases.items() if key and value}

    for session_id, entry in list(overrides.items()):
        if "alias" in entry:
            entry.pop("alias", None)
        if entry:
            overrides[session_id] = entry
        else:
            overrides.pop(session_id, None)

    for session_id, alias in desired_aliases.items():
        entry = dict(overrides.get(session_id, {}))
        entry["alias"] = alias
        overrides[session_id] = entry

    save_overrides(aliases_db, overrides)


def set_override_field(
    overrides_db: Path,
    session_id: str,
    field: str,
    value: str,
) -> str:
    if field not in OVERRIDE_FIELDS:
        raise ValueError(f"Unsupported override field: {field}")

    overrides = load_overrides(overrides_db)
    entry = dict(overrides.get(session_id, {}))
    previous = str(entry.get(field) or "")
    normalized = str(value or "").strip()

    if normalized:
        entry[field] = normalized
        overrides[session_id] = entry
    else:
        entry.pop(field, None)
        if entry:
            overrides[session_id] = entry
        else:
            overrides.pop(session_id, None)

    save_overrides(overrides_db, overrides)
    return previous


def clear_session_overrides(overrides_db: Path, session_id: str) -> Dict[str, str]:
    overrides = load_overrides(overrides_db)
    previous = dict(overrides.pop(session_id, {}))
    save_overrides(overrides_db, overrides)
    return previous


def update_slack_repo_for_session(slack_db: Optional[Path], session_id: str, cwd: str) -> List[str]:
    if not slack_db or not slack_db.exists():
        return []
    try:
        payload = json.loads(slack_db.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []

    normalized_cwd = str(cwd).strip()
    changed_threads: List[str] = []
    for thread_key, raw_value in payload.items():
        if not isinstance(raw_value, dict):
            continue
        mapped_session_id = str(raw_value.get("last_session_id") or "").strip()
        if mapped_session_id != session_id:
            continue
        previous_repo = str(raw_value.get("repo") or "").strip()
        if previous_repo == normalized_cwd:
            continue
        raw_value["repo"] = normalized_cwd
        changed_threads.append(str(thread_key))

    if changed_threads:
        slack_db.parent.mkdir(parents=True, exist_ok=True)
        slack_db.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return changed_threads


def update_thread_state_metadata(
    codex_home: Path,
    session_id: str,
    *,
    source: Optional[str] = None,
    title: Optional[str] = None,
    cwd: Optional[str] = None,
    updated_at: Optional[int] = None,
) -> Dict[str, Any]:
    db_path = codex_home / "state_5.sqlite"
    result: Dict[str, Any] = {
        "thread_state_db_found": db_path.exists(),
        "thread_state_row_found": False,
        "thread_state_changed": False,
        "thread_state_error": "",
        "previous_client_source": "",
        "previous_state_title": "",
        "previous_state_cwd": "",
        "previous_state_updated_at": 0,
        "client_source": "",
        "state_title": "",
        "state_cwd": "",
        "state_updated_at": 0,
    }
    if not db_path.exists():
        return result

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        result["thread_state_error"] = str(exc)
        return result

    try:
        row = conn.execute(
            "SELECT source, title, cwd, updated_at FROM threads WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return result

        result["thread_state_row_found"] = True
        previous_source = str(row["source"] or "").strip()
        previous_title = str(row["title"] or "").strip()
        previous_cwd = str(row["cwd"] or "").strip()
        previous_updated_at = int(row["updated_at"] or 0)
        result["previous_client_source"] = previous_source
        result["previous_state_title"] = previous_title
        result["previous_state_cwd"] = previous_cwd
        result["previous_state_updated_at"] = previous_updated_at

        current_source = previous_source
        current_title = previous_title
        current_cwd = previous_cwd
        current_updated_at = previous_updated_at
        updates: List[str] = []
        params: List[Any] = []

        if source is not None:
            normalized_source = str(source or "").strip()
            if normalized_source != previous_source:
                updates.append("source = ?")
                params.append(normalized_source)
                current_source = normalized_source

        if title is not None:
            normalized_title = str(title or "").strip()
            if normalized_title != previous_title:
                updates.append("title = ?")
                params.append(normalized_title)
                current_title = normalized_title

        if cwd is not None:
            normalized_cwd = str(cwd or "").strip()
            if normalized_cwd != previous_cwd:
                updates.append("cwd = ?")
                params.append(normalized_cwd)
                current_cwd = normalized_cwd

        if updated_at is not None:
            normalized_updated_at = max(0, int(updated_at))
            if normalized_updated_at != previous_updated_at:
                updates.append("updated_at = ?")
                params.append(normalized_updated_at)
                current_updated_at = normalized_updated_at

        if updates:
            params.append(session_id)
            conn.execute(f"UPDATE threads SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
            result["thread_state_changed"] = True

        result["client_source"] = current_source
        result["state_title"] = current_title
        result["state_cwd"] = current_cwd
        result["state_updated_at"] = current_updated_at
        return result
    except Exception as exc:
        result["thread_state_error"] = str(exc)
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def sync_official_title_with_app_server(
    session_id: str,
    title: str,
    *,
    codex_home: Optional[Path],
    codex_bin: str = "codex",
    timeout_seconds: float = 30.0,
) -> Dict[str, Any]:
    normalized_title = str(title or "").strip()
    if not normalized_title:
        return build_official_title_sync_result(
            attempted=False,
            status="skipped_empty_title",
        )

    resolved_bin = shutil.which(codex_bin)
    if not resolved_bin:
        return build_official_title_sync_result(
            attempted=True,
            status="failed",
            error=f"codex binary not found: {codex_bin}",
        )

    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = str(codex_home)

    try:
        process = subprocess.Popen(
            [resolved_bin, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
    except Exception as exc:
        return build_official_title_sync_result(
            attempted=True,
            status="failed",
            error=str(exc),
        )

    def send_jsonrpc(message: Dict[str, Any]) -> None:
        if not process.stdin:
            raise RuntimeError("codex app-server stdin is unavailable")
        process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    responses: Dict[str, Dict[str, Any]] = {}
    stderr_tail: List[str] = []

    try:
        send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "codex-sessions", "version": "1.0.0"},
                },
            }
        )
        send_jsonrpc({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "2",
                "method": "thread/resume",
                "params": {"threadId": session_id},
            }
        )
        send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "3",
                "method": "thread/name/set",
                "params": {"threadId": session_id, "name": normalized_title},
            }
        )
    except Exception as exc:
        try:
            process.terminate()
        except Exception:
            pass
        return build_official_title_sync_result(
            attempted=True,
            status="failed",
            error=str(exc),
        )

    stdout_buffer = ""
    stderr_buffer = ""
    stdout_fd = process.stdout.fileno() if process.stdout else None
    stderr_fd = process.stderr.fileno() if process.stderr else None
    fds = [fd for fd in [stdout_fd, stderr_fd] if fd is not None]

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if "3" in responses:
            break

        if not fds:
            break

        try:
            ready, _, _ = select.select(fds, [], [], 0.2)
        except Exception:
            ready = []

        if not ready:
            if process.poll() is not None:
                break
            continue

        for fd in ready:
            try:
                chunk = os.read(fd, 65536)
            except Exception:
                chunk = b""
            if not chunk:
                continue

            text = chunk.decode("utf-8", errors="replace")

            if fd == stderr_fd:
                stderr_buffer += text
                lines = stderr_buffer.split("\n")
                stderr_buffer = lines[-1]
                for raw_line in lines[:-1]:
                    value = raw_line.strip()
                    if not value:
                        continue
                    stderr_tail.append(value)
                    if len(stderr_tail) > 6:
                        stderr_tail = stderr_tail[-6:]
                continue

            stdout_buffer += text
            lines = stdout_buffer.split("\n")
            stdout_buffer = lines[-1]
            for raw_line in lines[:-1]:
                raw = raw_line.strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except Exception:
                    continue
                response_id = message.get("id")
                if response_id is None:
                    continue
                responses[str(response_id)] = message

    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=1.0)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass

    response = responses.get("3")
    if response and "result" in response:
        return build_official_title_sync_result(
            attempted=True,
            status="ok",
        )

    if response and "error" in response:
        err = response.get("error") or {}
        return build_official_title_sync_result(
            attempted=True,
            status="failed",
            error=str(err.get("message") or err),
        )

    resume_response = responses.get("2")
    if resume_response and "error" in resume_response:
        err = resume_response.get("error") or {}
        detail = str(err.get("message") or err)
    else:
        detail = "thread/name/set response timed out"
    if stderr_tail:
        detail = f"{detail}; stderr: {' | '.join(stderr_tail)}"

    return build_official_title_sync_result(
        attempted=True,
        status="failed",
        error=detail,
    )


def sync_thread_name_with_app_server(
    session_id: str,
    title: str,
    *,
    codex_home: Optional[Path],
    codex_bin: str = "codex",
    timeout_seconds: float = 30.0,
) -> Dict[str, Any]:
    return sync_official_title_with_app_server(
        session_id=session_id,
        title=title,
        codex_home=codex_home,
        codex_bin=codex_bin,
        timeout_seconds=timeout_seconds,
    )


def discover_vscode_codex_bins() -> List[str]:
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=False,
        )
        output = completed.stdout or ""
    except Exception:
        return []

    candidates: List[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"^\s*(\d+)\s+(.*/\.vscode-server/extensions/openai\.chatgpt[^ ]*/bin/linux-x86_64/codex)\s+app-server\b"
    )
    for line in output.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        pid = match.group(1)
        codex_real_bin = match.group(2)
        if not os.path.isfile(codex_real_bin):
            continue

        lock_link = f"/proc/{pid}/fd/3"
        try:
            lock_path = os.path.realpath(lock_link)
        except Exception:
            continue
        if not lock_path.endswith("/.lock"):
            continue

        arg0_dir = os.path.dirname(lock_path)
        if "/.codex/tmp/arg0/" not in arg0_dir:
            continue

        arg0_codex = os.path.join(arg0_dir, "codex")
        try:
            if not os.path.exists(arg0_codex):
                os.symlink(codex_real_bin, arg0_codex)
        except Exception:
            # best-effort; if we cannot create the helper symlink, skip this candidate
            continue

        if os.path.isfile(arg0_codex) and os.access(arg0_codex, os.X_OK) and arg0_codex not in seen:
            candidates.append(arg0_codex)
            seen.add(arg0_codex)

    return candidates


def sync_official_title_to_targets(
    session_id: str,
    title: str,
    *,
    codex_home: Optional[Path],
    primary_codex_bin: str,
) -> Dict[str, Any]:
    targets: List[str] = [primary_codex_bin]
    for extra_bin in discover_vscode_codex_bins():
        if extra_bin not in targets:
            targets.append(extra_bin)

    target_results: List[Dict[str, str]] = []
    any_attempted = False
    any_ok = False
    error_messages: List[str] = []

    for target in targets:
        result = sync_official_title_with_app_server(
            session_id=session_id,
            title=title,
            codex_home=codex_home,
            codex_bin=target,
            timeout_seconds=12.0 if target != primary_codex_bin else 30.0,
        )
        status = str(result.get("official_title_sync") or result.get("thread_name_sync") or "")
        attempted = bool(
            result.get("official_title_sync_attempted")
            if "official_title_sync_attempted" in result
            else result.get("thread_name_sync_attempted")
        )
        error = str(
            result.get("official_title_sync_error")
            or result.get("thread_name_sync_error")
            or ""
        ).strip()
        target_results.append({"bin": target, "status": status, "error": error})
        if attempted:
            any_attempted = True
        if status == "ok":
            any_ok = True
        elif status == "failed" and error:
            error_messages.append(f"{target}: {error}")

    if any_ok:
        return build_official_title_sync_result(
            attempted=any_attempted,
            status="ok",
            targets=target_results,
        )
    if any_attempted:
        return build_official_title_sync_result(
            attempted=True,
            status="failed",
            error=" | ".join(error_messages[:4]) if error_messages else "unknown error",
            targets=target_results,
        )
    return build_official_title_sync_result(
        attempted=False,
        status="skipped",
        targets=target_results,
    )


def sync_thread_name_to_targets(
    session_id: str,
    title: str,
    *,
    codex_home: Optional[Path],
    primary_codex_bin: str,
) -> Dict[str, Any]:
    return sync_official_title_to_targets(
        session_id=session_id,
        title=title,
        codex_home=codex_home,
        primary_codex_bin=primary_codex_bin,
    )


def set_session_title(
    record: SessionRecord,
    aliases_db: Path,
    title: str,
    *,
    codex_bin: str = "codex",
    codex_home: Optional[Path] = None,
) -> Dict[str, Any]:
    previous = update_session_metadata(record.path, title=title)
    preview_update = update_first_user_preview(record.path, title)
    previous.update(preview_update)
    previous["override_title"] = set_override_field(
        aliases_db,
        record.session_id,
        "title",
        title,
    )
    effective_title = str(title or "").strip()
    if not effective_title:
        refreshed = read_jsonl_meta(record.path)
        effective_title = str(refreshed.title if refreshed else "").strip()
    previous["effective_title"] = effective_title
    sync_codex_home = codex_home or aliases_db.parent
    previous.update(
        sync_official_title_to_targets(
            session_id=record.session_id,
            title=effective_title,
            codex_home=sync_codex_home,
            primary_codex_bin=codex_bin,
        )
    )
    return previous


def set_session_cwd(
    record: SessionRecord,
    aliases_db: Path,
    slack_db: Optional[Path],
    cwd: str,
) -> Dict[str, Any]:
    previous = update_session_metadata(record.path, cwd=cwd)
    previous["override_cwd"] = set_override_field(
        aliases_db,
        record.session_id,
        "cwd",
        cwd,
    )
    previous["slack_threads"] = update_slack_repo_for_session(
        slack_db=slack_db,
        session_id=record.session_id,
        cwd=cwd,
    )
    return previous


def set_session_source(
    record: SessionRecord,
    source: str,
    *,
    codex_home: Path,
) -> Dict[str, Any]:
    normalized_source = str(source or "").strip()
    previous = update_session_metadata(record.path, source=normalized_source)
    effective_title = choose_display_title(
        record.vscode_display_name,
        record.thread_name,
        record.title,
    )
    if not effective_title:
        refreshed = read_jsonl_meta(record.path)
        effective_title = choose_display_title(
            getattr(refreshed, "title", ""),
            record.session_id,
        )
    previous.update(
        update_thread_state_metadata(
            codex_home=codex_home,
            session_id=record.session_id,
            source=normalized_source,
            title=effective_title,
            updated_at=int(time.time()),
        )
    )
    previous["effective_title"] = effective_title
    previous["effective_source"] = str(
        previous.get("client_source") or normalized_source or record.source
    ).strip()
    previous["session_index_appended"] = append_session_index_entry(
        codex_home,
        record.session_id,
        effective_title,
    )
    return previous


def load_records(
    codex_home: Path,
    include_archived: bool,
    slack_db: Optional[Path],
    aliases_db: Optional[Path],
) -> List[SessionRecord]:
    slack_index = load_slack_index(slack_db)
    override_index = load_overrides(aliases_db)
    thread_state_index = load_thread_state_index(codex_home)
    thread_name_index = load_thread_name_index(codex_home)
    vscode_task_title_index = load_vscode_task_title_index(codex_home)
    records: List[SessionRecord] = []
    for path in iter_session_files(codex_home=codex_home, include_archived=include_archived):
        record = read_jsonl_meta(path)
        if not record:
            continue
        state_entry = thread_state_index.get(record.session_id, {})
        record.session_source = str(record.source or "").strip()
        record.client_source = str(state_entry.get("source") or "").strip()
        record.source = record.client_source or record.session_source
        record.thread_name = str(thread_name_index.get(record.session_id) or "").strip()
        if not record.thread_name:
            record.thread_name = str(state_entry.get("title") or "").strip()
        override_entry = override_index.get(record.session_id, {})
        record.alias = str(override_entry.get("alias") or "").strip()
        title_override = str(override_entry.get("title") or "").strip()
        if title_override:
            record.title = title_override
        cwd_override = str(override_entry.get("cwd") or "").strip()
        if cwd_override:
            record.cwd = cwd_override
        record.vscode_display_name = choose_display_title(
            str(vscode_task_title_index.get(record.session_id) or "").strip(),
            record.thread_name,
            record.title,
            record.session_id,
        )
        relation = extract_subagent_relationship(
            record.client_source,
            record.source,
            record.session_source,
        )
        record.parent_session_id = str(relation.get("parent_session_id") or "").strip()
        record.subagent_nickname = str(relation.get("subagent_nickname") or "").strip()
        record.subagent_role = str(relation.get("subagent_role") or "").strip()
        record.subagent_depth = int(relation.get("subagent_depth") or 0)
        record.parent_display_title = ""
        record.slack_threads = sorted(slack_index.get(record.session_id, []))
        records.append(record)
    record_index = {record.session_id: record for record in records}
    for record in records:
        if not record.parent_session_id:
            continue
        parent = record_index.get(record.parent_session_id)
        if parent:
            record.parent_display_title = session_record_display_name(parent)
            continue
        parent_state = thread_state_index.get(record.parent_session_id, {})
        record.parent_display_title = choose_display_title(
            str(vscode_task_title_index.get(record.parent_session_id) or "").strip(),
            str(thread_name_index.get(record.parent_session_id) or "").strip(),
            str(parent_state.get("title") or "").strip(),
            record.parent_session_id,
        )
    records.sort(key=lambda item: item.timestamp, reverse=True)
    return records


def filter_records(
    records: List[SessionRecord],
    query: str,
    source_label: str = "",
) -> List[SessionRecord]:
    filtered = records

    label_needle = source_label.strip().lower()
    if label_needle:
        filtered = [
            record
            for record in filtered
            if label_needle in build_source_search_blob(record.originator, record.source).lower()
        ]

    if not query:
        return filtered

    needle = query.lower()
    matched = []
    for record in filtered:
        haystack = " ".join(
            [
                record.session_id,
                record.timestamp,
                record.cwd,
                record.originator,
                record.source,
                record.session_source,
                record.client_source,
                build_source_search_blob(record.originator, record.source),
                record.model_provider,
                record.model,
                record.effort,
                record.title,
                record.thread_name,
                record.vscode_display_name,
                record.alias,
                record.parent_session_id,
                record.parent_display_title,
                record.subagent_nickname,
                record.subagent_role,
                str(record.subagent_depth or ""),
                str(record.session_size_bytes),
                " ".join(record.slack_threads),
            ]
        ).lower()
        if needle in haystack:
            matched.append(record)
    return matched


def short(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_size(num_bytes: int) -> str:
    size = max(0, int(num_bytes or 0))
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}{unit}"
            if value >= 100:
                return f"{value:.0f}{unit}"
            if value >= 10:
                return f"{value:.1f}{unit}"
            return f"{value:.2f}{unit}"
        value /= 1024.0
    return f"{size}B"


def find_record(records: List[SessionRecord], session_id_or_prefix: str) -> SessionRecord:
    key = session_id_or_prefix.strip()
    alias_exact = [record for record in records if record.alias == key]
    if len(alias_exact) == 1:
        return alias_exact[0]
    if len(alias_exact) > 1:
        raise ValueError(f"Alias is ambiguous: {key}")

    exact = [record for record in records if record.session_id == key]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError(f"Multiple exact matches found for {key}")

    matches = [record for record in records if record.session_id.startswith(key)]
    if not matches:
        raise ValueError(f"Session not found: {key}")
    if len(matches) > 1:
        raise ValueError(
            "Session prefix is ambiguous. Matches: "
            + ", ".join(record.session_id for record in matches[:8])
        )
    return matches[0]


def print_list(records: List[SessionRecord], limit: int) -> None:
    print("time | id | alias | title | size | src | model | effort | state | workdir | slack")
    count = 0
    for record in records:
        if count >= limit:
            break
        state = "archived" if record.archived else "active"
        src = describe_source(record.originator, record.source)["label"]
        slack = ",".join(record.slack_threads) if record.slack_threads else "-"
        alias = record.alias or "-"
        title = (
            choose_display_title(
                record.vscode_display_name,
                record.thread_name,
                record.title,
                record.session_id,
            )
            or "-"
        )
        size = format_size(record.session_size_bytes)
        print(
            f"{record.timestamp} | {record.session_id} | {short(alias, 20)} | {short(title, 42)} | {size} | {src} | "
            f"{short(record.model or '-', 22)} | {record.effort or '-'} | {state} | "
            f"{short(display_path(record.cwd) if record.cwd else '-', 80)} | {short(slack, 42)}"
        )
        count += 1
    print(f"\nlisted: {min(len(records), limit)} / total: {len(records)}")


def cmd_list(args: argparse.Namespace) -> int:
    records = load_records(
        codex_home=args.codex_home,
        include_archived=args.archived,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    records = filter_records(records, args.grep, args.source_label)
    if args.json:
        payload = [asdict(record) for record in records[: args.limit]]
        for item in payload:
            item["path"] = str(item["path"])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print_list(records, args.limit)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    records = filter_records(records, "", getattr(args, "source_label", ""))
    active = sum(1 for record in records if not record.archived)
    archived = sum(1 for record in records if record.archived)
    originator = Counter(record.originator or "unknown" for record in records)
    source = Counter(describe_source(record.originator, record.source)["long_label"] for record in records)
    model = Counter(record.model or "unknown" for record in records)
    effort = Counter(record.effort or "unknown" for record in records)
    with_slack = sum(1 for record in records if record.slack_threads)
    with_alias = sum(1 for record in records if record.alias)

    print(f"now: {iso_now()}")
    print(f"total: {len(records)}")
    print(f"active: {active}")
    print(f"archived: {archived}")
    print(f"mapped_to_slack_thread: {with_slack}")
    print(f"with_alias: {with_alias}")
    print("\noriginator:")
    for key, value in originator.most_common():
        print(f"- {key}: {value}")
    print("\nsource:")
    for key, value in source.most_common():
        print(f"- {key}: {value}")
    print("\nmodel:")
    for key, value in model.most_common():
        print(f"- {key}: {value}")
    print("\neffort:")
    for key, value in effort.most_common():
        print(f"- {key}: {value}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    record = find_record(records, args.session_id)

    print(f"id: {record.session_id}")
    print(f"local_alias: {record.alias or '(none)'}")
    print(f"session_title: {record.title or '(none)'}")
    print(f"official_title: {record.thread_name or '(none)'}")
    print(f"display_name: {session_record_display_name(record)}")
    print(f"time: {record.timestamp}")
    print(f"state: {'archived' if record.archived else 'active'}")
    print(f"path: {display_path(record.path)}")
    print(f"size: {format_size(record.session_size_bytes)} ({record.session_size_bytes} bytes)")
    print(f"workdir: {display_path(record.cwd) if record.cwd else '(none)'}")
    print(f"originator: {record.originator}")
    print(f"source_profile: {describe_source(record.originator, record.source)['long_label']}")
    print(f"effective_source: {record.source or '(none)'}")
    print(f"session_source: {record.session_source or '(none)'}")
    print(f"client_source: {record.client_source or '(none)'}")
    print(f"parent_session_id: {record.parent_session_id or '(none)'}")
    print(f"parent_display_name: {record.parent_display_title or '(none)'}")
    print(f"subagent_nickname: {record.subagent_nickname or '(none)'}")
    print(f"subagent_role: {record.subagent_role or '(none)'}")
    print(f"subagent_depth: {record.subagent_depth or 0}")
    print(f"cli_version: {record.cli_version}")
    print(f"model_provider: {record.model_provider}")
    print(f"model: {record.model}")
    print(f"effort: {record.effort}")
    print(
        "slack_threads: "
        + (", ".join(record.slack_threads) if record.slack_threads else "(none)")
    )

    if args.tail <= 0:
        return 0

    lines = record.path.read_text(encoding="utf-8").splitlines()
    tail_lines = lines[-args.tail :]
    print(f"\nlast {len(tail_lines)} jsonl lines:")
    for line in tail_lines:
        print(line)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    record = find_record(records, args.session_id)
    codex_bin = args.codex_bin

    if args.non_interactive:
        cmd = [codex_bin, "exec", "resume", "--skip-git-repo-check", "--all", record.session_id]
        if args.prompt:
            cmd.append(args.prompt)
    else:
        cmd = [codex_bin, "resume", "--all", record.session_id]
        if args.prompt:
            cmd.append(args.prompt)

    if args.print_cmd:
        print(" ".join(shlex_quote(token) for token in cmd))
        return 0

    completed = subprocess.run(cmd, check=False)
    return completed.returncode


def cmd_set_alias(args: argparse.Namespace) -> int:
    alias = args.alias.strip()
    if not alias:
        print("Alias cannot be empty")
        return 1

    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    record = find_record(records, args.session_id)
    aliases = load_aliases(args.aliases_db)

    conflict_sid = ""
    for sid, label in aliases.items():
        if sid != record.session_id and label == alias:
            conflict_sid = sid
            break
    if conflict_sid and not args.force:
        print(f"Alias `{alias}` already used by {conflict_sid}. Use --force to reassign.")
        return 1
    if conflict_sid:
        aliases.pop(conflict_sid, None)

    previous = aliases.get(record.session_id, "")
    aliases[record.session_id] = alias
    save_aliases(args.aliases_db, aliases)
    if previous:
        print(f"Updated alias for {record.session_id}: {previous} -> {alias}")
    else:
        print(f"Set alias for {record.session_id}: {alias}")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    return cmd_set_alias(args)


def cmd_clear_alias(args: argparse.Namespace) -> int:
    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    record = find_record(records, args.session_id)
    aliases = load_aliases(args.aliases_db)
    previous = aliases.pop(record.session_id, "")
    if not previous:
        print(f"No alias set for {record.session_id}")
        return 0
    save_aliases(args.aliases_db, aliases)
    print(f"Removed alias for {record.session_id}: {previous}")
    return 0


def cmd_unname(args: argparse.Namespace) -> int:
    return cmd_clear_alias(args)


def cmd_set_title(args: argparse.Namespace) -> int:
    title = args.title.strip()
    if not title:
        print("Title cannot be empty")
        return 1
    if len(title) > 140:
        print("Title too long (max 140)")
        return 1

    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    record = find_record(records, args.session_id)
    result = set_session_title(
        record,
        args.aliases_db,
        title,
        codex_bin=args.codex_bin,
        codex_home=args.codex_home,
    )
    previous = str(result.get("title") or result.get("override_title") or "").strip()
    preview_changed = bool(result.get("preview_changed"))
    sync_status = str(result.get("official_title_sync") or result.get("thread_name_sync") or "")
    sync_error = str(
        result.get("official_title_sync_error")
        or result.get("thread_name_sync_error")
        or ""
    ).strip()
    if previous == title and not preview_changed:
        print(f"Title unchanged for {record.session_id}: {title}")
        if sync_status == "ok":
            print("Synced official session title through Codex app-server.")
        elif sync_status == "failed":
            print(f"Official title sync failed: {sync_error or 'unknown error'}")
        return 0
    if previous == title and preview_changed:
        print(f"Title metadata unchanged for {record.session_id}: {title}")
        print("Updated first user prompt preview for VSCode conversation list.")
        if sync_status == "ok":
            print("Synced official session title through Codex app-server.")
        elif sync_status == "failed":
            print(f"Official title sync failed: {sync_error or 'unknown error'}")
        return 0
    if previous:
        print(f"Updated title for {record.session_id}: {previous} -> {title}")
    else:
        print(f"Set title for {record.session_id}: {title}")
    if sync_status == "ok":
        print("Synced official session title through Codex app-server.")
    elif sync_status == "failed":
        print(f"Official title sync failed: {sync_error or 'unknown error'}")
    return 0


def cmd_clear_title(args: argparse.Namespace) -> int:
    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    record = find_record(records, args.session_id)
    result = set_session_title(
        record,
        args.aliases_db,
        "",
        codex_bin=args.codex_bin,
        codex_home=args.codex_home,
    )
    previous = str(result.get("title") or result.get("override_title") or "").strip()
    effective_title = str(result.get("effective_title") or "").strip()
    sync_status = str(result.get("official_title_sync") or result.get("thread_name_sync") or "")
    sync_error = str(
        result.get("official_title_sync_error")
        or result.get("thread_name_sync_error")
        or ""
    ).strip()
    if previous:
        print(f"Cleared title for {record.session_id}: {previous}")
    else:
        print(f"No custom title for {record.session_id}")
    if effective_title:
        print(f"Effective title is now: {effective_title}")
    if sync_status == "ok":
        print("Synced effective official title through Codex app-server.")
    elif sync_status == "failed":
        print(f"Official title sync failed: {sync_error or 'unknown error'}")
    return 0


def cmd_set_source(args: argparse.Namespace) -> int:
    source = args.source.strip()
    if not source:
        print("Source cannot be empty")
        return 1
    if "\n" in source or "\r" in source:
        print("Source cannot contain newlines")
        return 1
    if len(source) > 1024:
        print("Source too long (max 1024)")
        return 1

    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    record = find_record(records, args.session_id)
    result = set_session_source(
        record,
        source,
        codex_home=args.codex_home,
    )
    previous_session_source = str(result.get("source") or "").strip()
    previous_client_source = str(result.get("previous_client_source") or "").strip()
    effective_source = str(result.get("effective_source") or source).strip()
    thread_state_row_found = bool(result.get("thread_state_row_found"))
    thread_state_error = str(result.get("thread_state_error") or "").strip()
    session_index_appended = bool(result.get("session_index_appended"))

    if previous_session_source == source and previous_client_source == source:
        print(f"Source unchanged for {record.session_id}: {source}")
    elif previous_session_source and previous_session_source != source:
        print(f"Updated session source for {record.session_id}: {previous_session_source} -> {source}")
    else:
        print(f"Set session source for {record.session_id}: {source}")

    if thread_state_error:
        print(f"state_5.sqlite update failed: {thread_state_error}")
    elif thread_state_row_found:
        if previous_client_source == source:
            print(f"Client-visible source already set in state_5.sqlite: {source}")
        elif previous_client_source:
            print(f"Updated client-visible source in state_5.sqlite: {previous_client_source} -> {source}")
        else:
            print(f"Set client-visible source in state_5.sqlite: {source}")
    else:
        print("No matching thread row in state_5.sqlite; client visibility may remain unchanged.")

    print(f"Effective source is now: {effective_source}")
    if session_index_appended:
        print("Appended a fresh session_index.jsonl entry for client visibility refresh.")
    print("Note: this tool's own CLI lists sessions from disk directly; `source` mainly affects VS Code and the official Codex client picker.")
    return 0


def cmd_set_workdir(args: argparse.Namespace) -> int:
    cwd = args.cwd.strip()
    if not cwd:
        print("workdir cannot be empty")
        return 1
    if "\n" in cwd or "\r" in cwd:
        print("workdir cannot contain newlines")
        return 1
    if len(cwd) > 1024:
        print("workdir too long (max 1024)")
        return 1

    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    record = find_record(records, args.session_id)
    result = set_session_cwd(
        record=record,
        aliases_db=args.aliases_db,
        slack_db=args.slack_db,
        cwd=cwd,
    )
    previous = str(result.get("cwd") or result.get("override_cwd") or "").strip()
    updated_threads = result.get("slack_threads", [])
    if previous == cwd and not updated_threads:
        print(f"workdir unchanged for {record.session_id}: {cwd}")
        return 0
    if previous and previous != cwd:
        print(f"Updated workdir for {record.session_id}: {previous} -> {cwd}")
    elif previous == cwd:
        print(f"workdir metadata already set for {record.session_id}: {cwd}")
    else:
        print(f"Set workdir for {record.session_id}: {cwd}")
    if updated_threads:
        print(f"Updated Slack thread bindings: {', '.join(updated_threads)}")
    return 0


def cmd_set_cwd(args: argparse.Namespace) -> int:
    return cmd_set_workdir(args)


def cmd_clear_workdir(args: argparse.Namespace) -> int:
    _ = args
    print("clear-workdir is not supported. Use set-workdir to set the desired workdir.")
    return 1


def cmd_clear_cwd(args: argparse.Namespace) -> int:
    return cmd_clear_workdir(args)


def cmd_list_aliases(args: argparse.Namespace) -> int:
    aliases = load_aliases(args.aliases_db)
    if not aliases:
        print("No aliases")
        return 0

    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    by_id = {record.session_id: record for record in records}

    print("alias | id | state | workdir")
    for session_id, alias in sorted(aliases.items(), key=lambda item: item[1].lower()):
        record = by_id.get(session_id)
        if record:
            state = "archived" if record.archived else "active"
            cwd = short(display_path(record.cwd) if record.cwd else "-", 80)
        else:
            state = "missing"
            cwd = "-"
        print(f"{alias} | {session_id} | {state} | {cwd}")
    return 0


def cmd_aliases(args: argparse.Namespace) -> int:
    return cmd_list_aliases(args)


def cmd_archive(args: argparse.Namespace) -> int:
    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    record = find_record(records, args.session_id)
    if record.archived:
        print(f"Already archived: {record.session_id}")
        return 0

    dst_dir = args.codex_home / "archived_sessions"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / record.path.name
    if dst_path.exists():
        print(f"Archive target already exists: {display_path(dst_path)}")
        return 1

    shutil.move(str(record.path), str(dst_path))
    print(f"Archived: {record.session_id}")
    print(f"from: {display_path(record.path)}")
    print(f"to:   {display_path(dst_path)}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    if not args.yes:
        print("Refusing to delete without --yes")
        return 1

    records = load_records(
        codex_home=args.codex_home,
        include_archived=True,
        slack_db=args.slack_db,
        aliases_db=args.aliases_db,
    )
    record = find_record(records, args.session_id)
    record.path.unlink(missing_ok=False)
    clear_session_overrides(args.aliases_db, record.session_id)
    print(f"Deleted: {record.session_id}")
    print(f"path: {display_path(record.path)}")
    return 0


def cmd_paths(args: argparse.Namespace) -> int:
    print(f"codex_home: {display_path(args.codex_home)}")
    print(f"sessions: {display_path(args.codex_home / 'sessions')}")
    print(f"archived_sessions: {display_path(args.codex_home / 'archived_sessions')}")
    print(f"state_db: {display_path(args.codex_home / 'state_5.sqlite')}")
    print(
        "overrides_db: "
        f"{display_path(args.aliases_db)} (alias/title/workdir overrides; legacy filename)"
    )
    if args.slack_db:
        print(f"slack_db: {display_path(args.slack_db)}")
    else:
        print("slack_db: (disabled)")
    return 0


def shlex_quote(text: str) -> str:
    if not text:
        return "''"
    if all(ch.isalnum() or ch in "-._/:=+" for ch in text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


def build_parser(
    default_codex_home: Path,
    default_slack_db: Optional[Path],
    default_aliases_db: Path,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex_sessions.py",
        description="List and manage local Codex sessions, aliases, titles, and workdirs across CLI, VSCode, and Slack gateway.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home,
        help="Codex home directory (default: ~/.codex)",
    )
    parser.add_argument(
        "--slack-db",
        type=Path,
        default=default_slack_db,
        help="Slack gateway sessions.json path (optional)",
    )
    parser.add_argument(
        "--aliases-db",
        "--overrides-db",
        dest="aliases_db",
        type=Path,
        default=default_aliases_db,
        help="Session override DB path (legacy default filename: ~/.codex/session_aliases.json)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List sessions")
    list_parser.add_argument("--archived", action="store_true", help="Include archived sessions")
    list_parser.add_argument("--limit", type=int, default=50, help="Max rows to print")
    list_parser.add_argument("--grep", default="", help="Filter by substring")
    list_parser.add_argument(
        "--source-label",
        default="",
        help="Filter by source target substring (example: vscode/cli/exec/subagent)",
    )
    list_parser.add_argument("--json", action="store_true", help="Output JSON")
    list_parser.set_defaults(func=cmd_list)

    stats_parser = subparsers.add_parser("stats", help="Show aggregated counts")
    stats_parser.add_argument(
        "--source-label",
        default="",
        help="Filter counts by source target substring (example: vscode/cli/exec/subagent)",
    )
    stats_parser.set_defaults(func=cmd_stats)

    show_parser = subparsers.add_parser("show", help="Show one session")
    show_parser.add_argument("session_id", help="Session id or unique prefix")
    show_parser.add_argument("--tail", type=int, default=0, help="Print last N jsonl lines")
    show_parser.set_defaults(func=cmd_show)

    resume_parser = subparsers.add_parser("resume", help="Resume a session with codex")
    resume_parser.add_argument("session_id", help="Session id or unique prefix")
    resume_parser.add_argument("--interactive", action="store_true", help=argparse.SUPPRESS)
    resume_parser.add_argument("--non-interactive", action="store_true", help="Use `codex exec resume`")
    resume_parser.add_argument("--print-cmd", action="store_true", help="Print command only")
    resume_parser.add_argument("--codex-bin", default="codex", help="Codex binary")
    resume_parser.add_argument("--prompt", default="", help="Optional prompt for resume command")
    resume_parser.set_defaults(func=cmd_resume)

    set_alias_parser = subparsers.add_parser(
        "set-alias",
        aliases=["rename"],
        help="Set or update a local alias for a session",
    )
    set_alias_parser.add_argument("session_id", help="Session id, id prefix, or existing alias")
    set_alias_parser.add_argument("alias", help="New local alias")
    set_alias_parser.add_argument("--force", action="store_true", help="Reassign alias if occupied")
    set_alias_parser.set_defaults(func=cmd_set_alias)

    clear_alias_parser = subparsers.add_parser(
        "clear-alias",
        aliases=["unname"],
        help="Remove the local alias from a session",
    )
    clear_alias_parser.add_argument("session_id", help="Session id, id prefix, or alias")
    clear_alias_parser.set_defaults(func=cmd_clear_alias)

    set_title_parser = subparsers.add_parser(
        "set-title",
        help="Set a custom session title and sync the official Codex thread title",
    )
    set_title_parser.add_argument("session_id", help="Session id, id prefix, or alias")
    set_title_parser.add_argument("title", help="Custom title")
    set_title_parser.add_argument("--codex-bin", default="codex", help="Codex binary")
    set_title_parser.set_defaults(func=cmd_set_title)

    clear_title_parser = subparsers.add_parser(
        "clear-title",
        help="Clear the custom session title and resync the derived official title",
    )
    clear_title_parser.add_argument("session_id", help="Session id, id prefix, or alias")
    clear_title_parser.add_argument("--codex-bin", default="codex", help="Codex binary")
    clear_title_parser.set_defaults(func=cmd_clear_title)

    set_source_parser = subparsers.add_parser(
        "set-source",
        help="Set the effective Codex client source used for client visibility (example: vscode/cli/exec)",
    )
    set_source_parser.add_argument("session_id", help="Session id, id prefix, or alias")
    set_source_parser.add_argument("source", help="Target source label")
    set_source_parser.set_defaults(func=cmd_set_source)

    set_workdir_parser = subparsers.add_parser(
        "set-workdir",
        aliases=["set-cwd"],
        help="Set the stored session workdir",
    )
    set_workdir_parser.add_argument("session_id", help="Session id, id prefix, or alias")
    set_workdir_parser.add_argument("cwd", help="Target workdir")
    set_workdir_parser.set_defaults(func=cmd_set_workdir)

    clear_workdir_parser = subparsers.add_parser(
        "clear-workdir",
        aliases=["clear-cwd"],
        help="Unsupported legacy-style clear operation",
    )
    clear_workdir_parser.add_argument("session_id", help="Session id, id prefix, or alias")
    clear_workdir_parser.set_defaults(func=cmd_clear_workdir)

    list_aliases_parser = subparsers.add_parser(
        "list-aliases",
        aliases=["aliases"],
        help="List local aliases",
    )
    list_aliases_parser.set_defaults(func=cmd_list_aliases)

    archive_parser = subparsers.add_parser("archive", help="Move a session to archived_sessions")
    archive_parser.add_argument("session_id", help="Session id or unique prefix")
    archive_parser.set_defaults(func=cmd_archive)

    delete_parser = subparsers.add_parser("delete", help="Delete a session file permanently")
    delete_parser.add_argument("session_id", help="Session id or unique prefix")
    delete_parser.add_argument("--yes", action="store_true", help="Required confirmation flag")
    delete_parser.set_defaults(func=cmd_delete)

    paths_parser = subparsers.add_parser("paths", help="Print storage paths")
    paths_parser.set_defaults(func=cmd_paths)

    return parser


def main() -> int:
    default_codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    default_aliases_db = default_codex_home / "session_aliases.json"
    default_slack_db_path = (
        Path(__file__).resolve().parent.parent / "sessions.json"
    )
    default_slack_db = default_slack_db_path if default_slack_db_path.exists() else None

    parser = build_parser(
        default_codex_home=default_codex_home,
        default_slack_db=default_slack_db,
        default_aliases_db=default_aliases_db,
    )
    args = parser.parse_args()
    args.codex_home = args.codex_home.expanduser().resolve()
    if args.slack_db:
        args.slack_db = args.slack_db.expanduser().resolve()
    args.aliases_db = args.aliases_db.expanduser().resolve()

    try:
        return int(args.func(args))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
