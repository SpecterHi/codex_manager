#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from codex_manager_release import (
    RELEASE_METADATA_FILENAME,
    build_release_metadata,
    version_label,
)


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


def iso_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap codex_manager onto a remote host over SSH + sudo."
    )
    parser.add_argument("--host", required=True, help="Remote SSH host")
    parser.add_argument("--user", required=True, help="Remote SSH user")
    parser.add_argument("--ssh-port", type=int, default=22, help="Remote SSH port")
    parser.add_argument("--label", default="", help="Friendly target label to store locally")
    parser.add_argument(
        "--remote-base",
        default="~/.local/share/codex_manager",
        help="Remote install root. '~' expands to the remote user's home.",
    )
    parser.add_argument("--bind-host", default="127.0.0.1", help="Bind host for the remote web service")
    parser.add_argument("--bind-port", type=int, default=8765, help="Bind port for the remote web service")
    parser.add_argument(
        "--codex-home",
        default="~/.codex",
        help="Remote CODEX_HOME. '~' expands to the remote user's home.",
    )
    parser.add_argument(
        "--service-name",
        default="codex-sessions-web.service",
        help="Remote systemd service name",
    )
    parser.add_argument(
        "--targets-file",
        type=Path,
        default=Path.home() / ".config" / "codex-sessions-web" / "targets.json",
        help="Local targets file to update",
    )
    parser.add_argument(
        "--skip-target-config",
        action="store_true",
        help="Do not write the local targets.json entry",
    )
    parser.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        help="Extra ssh/scp -o KEY=VALUE options",
    )
    return parser.parse_args()


@dataclass
class RemoteSpec:
    host: str
    user: str
    ssh_port: int
    label: str
    remote_base: str
    bind_host: str
    bind_port: int
    codex_home: str
    service_name: str

    @property
    def target_id(self) -> str:
        return f"{self.user.lower()}@{self.host.lower()}:{self.ssh_port}"

    @property
    def target_label(self) -> str:
        return self.label.strip() or self.target_id

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.bind_port}"


@dataclass
class RemoteIdentity:
    home: str
    user: str
    group: str


def build_ssh_base(spec: RemoteSpec, extra_options: Iterable[str]) -> list[str]:
    cmd = ["ssh", "-p", str(spec.ssh_port)]
    for option in extra_options:
        text = str(option).strip()
        if text:
            cmd.extend(["-o", text])
    cmd.append(f"{spec.user}@{spec.host}")
    return cmd


def build_scp_base(spec: RemoteSpec, extra_options: Iterable[str]) -> list[str]:
    cmd = ["scp", "-P", str(spec.ssh_port)]
    for option in extra_options:
        text = str(option).strip()
        if text:
            cmd.extend(["-o", text])
    return cmd


def run_local(
    cmd: list[str],
    *,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        cmd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=check,
    )


@contextlib.contextmanager
def ssh_auth_context(password: str) -> Any:
    secret = str(password or "")
    askpass_path: str | None = None
    env: dict[str, str] | None = None
    prefix: list[str] = []
    auth_args: list[str] = []
    try:
        if secret:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                prefix="codex-bootstrap-askpass-",
                delete=False,
            ) as handle:
                handle.write("#!/bin/sh\nprintf '%s\\n' \"$CODEX_BOOTSTRAP_SSH_PASSWORD\"\n")
                askpass_path = handle.name
            os.chmod(askpass_path, 0o700)
            env = os.environ.copy()
            env["DISPLAY"] = env.get("DISPLAY") or "codex-bootstrap-auth"
            env["SSH_ASKPASS"] = askpass_path
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["CODEX_BOOTSTRAP_SSH_PASSWORD"] = secret
            prefix = ["setsid"]
            auth_args = [
                "-o",
                "BatchMode=no",
                "-o",
                "PreferredAuthentications=password,keyboard-interactive",
                "-o",
                "PubkeyAuthentication=no",
            ]
        yield prefix, env, auth_args
    finally:
        if askpass_path:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass


def run_remote(
    spec: RemoteSpec,
    extra_options: Iterable[str],
    script: str,
    *,
    password: str = "",
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    with ssh_auth_context(password) as (prefix, env, auth_args):
        cmd = build_ssh_base(spec, extra_options)
        if auth_args:
            cmd[1:1] = auth_args
        cmd.extend(["bash", "-se"])
        return run_local([*prefix, *cmd], input_bytes=script.encode("utf-8"), env=env, check=check)


def copy_to_remote(
    spec: RemoteSpec,
    extra_options: Iterable[str],
    local_path: Path,
    remote_path: str,
    *,
    password: str = "",
) -> None:
    with ssh_auth_context(password) as (prefix, env, auth_args):
        cmd = build_scp_base(spec, extra_options)
        if auth_args:
            cmd[1:1] = auth_args
        cmd.extend([str(local_path), f"{spec.user}@{spec.host}:{remote_path}"])
        try:
            run_local([*prefix, *cmd], env=env)
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                "Remote upload failed.\n"
                f"STDOUT:\n{exc.stdout.decode('utf-8', errors='replace')}\n"
                f"STDERR:\n{exc.stderr.decode('utf-8', errors='replace')}"
            ) from exc


def tar_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = tarinfo.name
    parts = set(Path(name).parts)
    if parts & EXCLUDE_NAMES:
        return None
    if name.endswith(".pyc") or name.endswith(".pyo"):
        return None
    return tarinfo


def build_archive_file(repo_root: Path) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="codex_manager-", suffix=".tar.gz")
    os.close(fd)
    archive_path = Path(raw_path)
    try:
        with tarfile.open(archive_path, mode="w:gz") as tar:
            for path in sorted(repo_root.iterdir(), key=lambda item: item.name):
                if path.name in EXCLUDE_NAMES:
                    continue
                tar.add(path, arcname=path.name, recursive=True, filter=tar_filter)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return archive_path


def verify_repo_root(repo_root: Path) -> None:
    required = ["codex_sessions_web.py", "codex_sessions_web.sh", "codex_sessions.py", "README.md"]
    missing = [name for name in required if not (repo_root / name).exists()]
    if missing:
        raise SystemExit(f"Not a codex_manager repo: missing {', '.join(missing)}")


def resolve_remote_path(remote_home: str, path: str) -> str:
    text = str(path).strip()
    if not text:
        raise SystemExit("Remote paths must not be empty")
    if text == "~":
        return remote_home
    if text.startswith("~/"):
        return f"{remote_home}/{text[2:]}"
    if text.startswith("/"):
        return text
    return f"{remote_home}/{text}"


def detect_remote_identity(spec: RemoteSpec, extra_options: Iterable[str], password: str) -> RemoteIdentity:
    script = """set -euo pipefail
python3 - <<'PY'
import json, os, pwd, grp
user = pwd.getpwuid(os.getuid()).pw_name
group = grp.getgrgid(os.getgid()).gr_name
print(json.dumps({"home": os.path.expanduser("~"), "user": user, "group": group}, ensure_ascii=False))
PY
"""
    try:
        result = run_remote(spec, extra_options, script, password=password)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"Failed to detect remote identity: {stderr or exc}") from exc
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="replace").strip())
    except Exception as exc:
        raise SystemExit(f"Failed to parse remote identity payload: {exc}") from exc
    remote_home = str(payload.get("home") or "").strip()
    remote_user = str(payload.get("user") or "").strip()
    remote_group = str(payload.get("group") or "").strip()
    if not remote_home.startswith("/") or not remote_user or not remote_group:
        raise SystemExit(f"Failed to detect a usable remote identity: {payload!r}")
    return RemoteIdentity(home=remote_home, user=remote_user, group=remote_group)


def ensure_remote_sudo(spec: RemoteSpec, extra_options: Iterable[str], password: str) -> None:
    script = """set -euo pipefail
sudo -n true
python3 --version >/dev/null
command -v curl >/dev/null
command -v tar >/dev/null
"""
    try:
        run_remote(spec, extra_options, script, password=password)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"Remote sudo/python/curl check failed: {stderr or exc}") from exc


def detect_remote_codex_bin(spec: RemoteSpec, extra_options: Iterable[str], password: str) -> str:
    script = r"""set -euo pipefail
python3 - <<'PY'
import json
import os
import shutil
from glob import glob

direct = shutil.which("codex")
if direct:
    print(json.dumps({"codex_bin": direct}, ensure_ascii=False))
    raise SystemExit(0)

candidates = [
    os.path.expanduser("~/.local/bin/codex"),
    os.path.expanduser("~/.npm-global/bin/codex"),
    os.path.expanduser("~/bin/codex"),
]
candidates.extend(sorted(glob(os.path.expanduser("~/.vscode-server/extensions/openai.chatgpt-*/bin/linux-x86_64/codex")), reverse=True))
candidates.extend(sorted(glob(os.path.expanduser("~/.vscode-server-insiders/extensions/openai.chatgpt-*/bin/linux-x86_64/codex")), reverse=True))
for candidate in candidates:
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        print(json.dumps({"codex_bin": candidate}, ensure_ascii=False))
        raise SystemExit(0)

print(json.dumps({"codex_bin": ""}, ensure_ascii=False))
PY
"""
    try:
        result = run_remote(spec, extra_options, script, password=password)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"Failed to detect remote codex binary: {stderr or exc}") from exc
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="replace").strip())
    except Exception as exc:
        raise SystemExit(f"Failed to parse remote codex payload: {exc}") from exc
    return str(payload.get("codex_bin") or "").strip()


def install_remote_release(
    spec: RemoteSpec,
    extra_options: Iterable[str],
    remote_identity: RemoteIdentity,
    archive_path: Path,
    *,
    release_metadata: dict[str, Any],
    remote_codex_bin: str,
    password: str,
) -> None:
    stamp = iso_stamp()
    remote_archive = f"/tmp/codex_manager-{stamp}.tar.gz"
    copy_to_remote(spec, extra_options, archive_path, remote_archive, password=password)
    base_abs = resolve_remote_path(remote_identity.home, spec.remote_base)
    codex_home_abs = resolve_remote_path(remote_identity.home, spec.codex_home)
    release_name = f"releases/{stamp}"
    metadata_json = json.dumps(
        {
            **release_metadata,
            "installed_at": datetime.now(tz=timezone.utc).isoformat(),
            "installed_via": "codex_sessions_bootstrap.py",
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    unit_text = f"""[Unit]
Description=Codex Sessions Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={remote_identity.user}
Group={remote_identity.group}
WorkingDirectory={base_abs}/current
Environment=PYTHONUNBUFFERED=1
Environment=CODEX_HOME={codex_home_abs}
Environment=PATH={remote_identity.home}/.local/bin:{remote_identity.home}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
ExecStart={base_abs}/current/codex_sessions_web.sh --host {spec.bind_host} --port {spec.bind_port} --codex-home {codex_home_abs}{f" --codex-bin {remote_codex_bin}" if remote_codex_bin else ""}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""
    script = f"""set -euo pipefail
BASE={shlex.quote(base_abs)}
ARCHIVE={shlex.quote(remote_archive)}
RELEASE="$BASE/{release_name}"
CURRENT="$BASE/current"
TMPDIR=$(mktemp -d)
cleanup() {{
  rm -rf "$TMPDIR"
  rm -f "$ARCHIVE"
}}
trap cleanup EXIT
mkdir -p "$BASE/releases"
rm -rf "$RELEASE"
mkdir -p "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"
chmod +x "$RELEASE/codex_sessions_web.sh" "$RELEASE/codex_sessions.sh" "$RELEASE/patch_vscode_codex_title_sync.py" || true
cat > "$RELEASE/{RELEASE_METADATA_FILENAME}" <<'JSON'
{metadata_json}
JSON
ln -sfn "$RELEASE" "$CURRENT"
cat > "$TMPDIR/{spec.service_name}" <<'UNIT'
{unit_text}UNIT
sudo install -m 0644 "$TMPDIR/{spec.service_name}" "/etc/systemd/system/{spec.service_name}"
sudo systemctl daemon-reload
sudo systemctl enable --now {spec.service_name}
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:{spec.bind_port}/api/remote_sessions >/dev/null; then
    sudo systemctl is-active {spec.service_name}
    exit 0
  fi
  sleep 1
done
echo "[bootstrap] remote service did not become ready in time" >&2
sudo systemctl status {spec.service_name} --no-pager -l || true
sudo journalctl -u {spec.service_name} -n 80 --no-pager || true
exit 1
"""
    try:
        run_remote(spec, extra_options, script, password=password)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        stdout = exc.stdout.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"Remote install failed.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}") from exc


def update_local_targets(path: Path, spec: RemoteSpec) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists():
        try:
            data: dict[str, Any] = json.loads(resolved.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list):
        raw_targets = []
    target = {
        "id": spec.target_id,
        "label": spec.target_label,
        "ssh_host": spec.host,
        "ssh_user": spec.user,
        "ssh_port": int(spec.ssh_port),
        "base_url": spec.base_url,
        "auth_mode": "key",
    }
    updated: list[dict[str, Any]] = []
    found = False
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "") == spec.target_id:
            updated.append(target)
            found = True
        else:
            updated.append(item)
    if not found:
        updated.append(target)
    data["targets"] = updated
    resolved.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(resolved, 0o600)


def verify_remote_proxy_path(spec: RemoteSpec, extra_options: Iterable[str], password: str) -> None:
    script = f"""set -euo pipefail
curl -fsS http://127.0.0.1:{spec.bind_port}/api/remote_sessions
"""
    try:
        result = run_remote(spec, extra_options, script, password=password)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"Remote verify failed: {stderr or exc}") from exc
    try:
        obj = json.loads(result.stdout.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise SystemExit(f"Remote verify returned non-JSON data: {exc}") from exc
    if obj.get("ok") is not True:
        raise SystemExit(f"Remote verify returned unexpected payload: {json.dumps(obj, ensure_ascii=False)}")
    summary = {
        "count": obj.get("count"),
        "auto_continue_count": obj.get("auto_continue_count"),
        "watchlist_count": obj.get("watchlist_count"),
    }
    sys.stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent
    verify_repo_root(repo_root)
    release_metadata = build_release_metadata(repo_root)
    spec = RemoteSpec(
        host=args.host.strip(),
        user=args.user.strip(),
        ssh_port=int(args.ssh_port),
        label=args.label.strip(),
        remote_base=args.remote_base.strip(),
        bind_host=args.bind_host.strip(),
        bind_port=int(args.bind_port),
        codex_home=args.codex_home.strip(),
        service_name=args.service_name.strip(),
    )
    if not spec.host or not spec.user:
        raise SystemExit("host/user required")
    archive_path = build_archive_file(repo_root)
    ssh_password = os.environ.get("CODEX_BOOTSTRAP_SSH_PASSWORD", "")
    try:
        print(f"[bootstrap] archive: {archive_path} ({archive_path.stat().st_size} bytes)")
        print(f"[bootstrap] local version: {version_label(release_metadata)}")
        print(f"[bootstrap] checking remote sudo/python on {spec.user}@{spec.host}:{spec.ssh_port}")
        ensure_remote_sudo(spec, args.ssh_option, ssh_password)
        remote_identity = detect_remote_identity(spec, args.ssh_option, ssh_password)
        print(
            f"[bootstrap] remote identity: user={remote_identity.user} group={remote_identity.group} home={remote_identity.home}"
        )
        remote_codex_bin = detect_remote_codex_bin(spec, args.ssh_option, ssh_password)
        print(f"[bootstrap] remote codex: {remote_codex_bin or '(not found; runtime fallback will try)'}")
        print(f"[bootstrap] installing release to {spec.remote_base} and enabling {spec.service_name}")
        install_remote_release(
            spec,
            args.ssh_option,
            remote_identity,
            archive_path,
            release_metadata=release_metadata,
            remote_codex_bin=remote_codex_bin,
            password=ssh_password,
        )
        print(f"[bootstrap] verifying remote API on {spec.base_url}")
        verify_remote_proxy_path(spec, args.ssh_option, ssh_password)
        if not args.skip_target_config:
            update_local_targets(args.targets_file, spec)
            print(f"[bootstrap] updated local targets file: {args.targets_file.expanduser().resolve()}")
        print("[bootstrap] done")
        return 0
    finally:
        archive_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
