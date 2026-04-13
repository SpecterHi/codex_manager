from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXCLUDE_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
}

RELEASE_METADATA_FILENAME = ".codex_manager_release.json"


def _iter_release_files(repo_root: Path):
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        if any(part in EXCLUDE_NAMES for part in rel.parts):
            continue
        if path.name == RELEASE_METADATA_FILENAME:
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        yield path, rel.as_posix()


def compute_repo_digest(repo_root: Path) -> str:
    digest = hashlib.sha256()
    for path, rel in _iter_release_files(repo_root):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def git_commit_info(repo_root: Path) -> dict[str, str]:
    try:
        full = (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL)
            .decode("utf-8", errors="replace")
            .strip()
        )
        short = (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL)
            .decode("utf-8", errors="replace")
            .strip()
        )
    except Exception:
        return {}
    if not full:
        return {}
    return {"git_commit": full, "git_commit_short": short or full[:7]}


def build_release_metadata(repo_root: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "content_digest": compute_repo_digest(repo_root),
    }
    metadata.update(git_commit_info(repo_root))
    return metadata


def version_label(metadata: dict[str, Any] | None) -> str:
    if not isinstance(metadata, dict):
        return "unknown"
    commit_short = str(metadata.get("git_commit_short") or "").strip()
    digest = str(metadata.get("content_digest") or "").strip()
    if commit_short and digest:
        return f"{commit_short} / {digest[:12]}"
    if commit_short:
        return commit_short
    if digest:
        return digest[:12]
    return "unknown"


def compare_release_metadata(local_meta: dict[str, Any] | None, remote_meta: dict[str, Any] | None) -> bool | None:
    if not isinstance(local_meta, dict) or not isinstance(remote_meta, dict):
        return None
    local_digest = str(local_meta.get("content_digest") or "").strip()
    remote_digest = str(remote_meta.get("content_digest") or "").strip()
    if local_digest and remote_digest:
        return local_digest == remote_digest
    local_commit = str(local_meta.get("git_commit") or "").strip()
    remote_commit = str(remote_meta.get("git_commit") or "").strip()
    if local_commit and remote_commit:
        return local_commit == remote_commit
    return None


def load_release_metadata(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
