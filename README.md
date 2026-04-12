# Codex Manager

`codex_manager` is a local session supervisor for Codex.

It focuses on three things:

- inspect and organize local Codex sessions
- watch active sessions through a bounded live event window instead of a full transcript dump
- continue or nudge sessions from a web UI, including a lightweight phone-oriented page

It can also proxy the same web actions to a small set of peer machines over SSH, so one control-plane page can observe and steer multiple Codex hosts.

## What It Does

`codex_manager` has two entrypoints:

- `codex_sessions.py`
  - CLI for listing, inspecting, renaming, archiving, deleting, and resuming sessions
- `codex_sessions_web.py`
  - local web UI with:
    - `/` full manager
    - `/remote` lightweight mobile-first dashboard

Core capabilities:

- session inventory with title, source, workdir, size, timestamps, and derived metadata
- live recent-event timeline for a selected session
- recent history fallback without loading entire giant `.jsonl` files
- safe-ish remote continue / stop controls for web-triggered runs
- multi-machine target switching over SSH
- optional local password auth for non-loopback access
- conservative auto-continue for long-running sessions that truly reached `task_complete`

## Why This Exists

The default Codex clients are good at one active conversation. They are not optimized for:

- scanning many sessions at once
- spotting which session is still moving, which is idle, and which likely needs intervention
- watching tool calls / commentary / completion markers across multiple threads
- nudging a session from a phone without opening a full desktop client

`codex_manager` is intentionally closer to a duty console than a transcript browser.

## Product Shape

### Full Manager: `/`

Desktop and tablet view:

- left side: fleet list for scanning many sessions
- right side: selected-session live console

The right side is a bounded sliding window. It emphasizes:

- commentary
- tool calls
- tool output summaries
- token / completion markers
- recent user/Codex turns when you explicitly ask for history

It does **not** try to load an entire huge session file by default.

### Lightweight Remote Page: `/remote`

Phone-first view for away-from-computer use:

- pinned watched sessions
- quick filters like `自动推进中`, `需人工介入`, `像是已完成`
- one-tap continue
- one-tap stop for web-triggered runs
- custom follow-up input
- recent 3-round context fallback
- optional `持续推进` mode

`持续推进` is conservative:

- checks every 3 minutes
- only triggers after an explicit `task_complete`
- records the last completed turn it already resumed
- does not use idle-time guessing

## Architecture

The runtime model is deliberately simple:

- session files are read from Codex home on disk
- recent live state is derived from session tails and recent structured events
- non-local targets are reached over SSH, then forwarded to the target machine's own loopback-bound web service

This means:

- the HTML comes from the control-plane machine
- the actual session operations still happen on the target machine
- password auth for SSH targets can be used from the browser, but those passwords stay in browser session storage and are not written into server-side target presets

## Naming Model

The tool keeps several naming concepts separate:

- `alias`
  - local nickname only
- `session title`
  - local title override stored in session metadata / preview
- `official title`
  - Codex thread title synced through `codex app-server` when available
- `display title`
  - best UI label, typically preferring VS Code task-list naming when present
- `source`
  - single client-facing thread source such as `vscode`, `cli`, or `exec`

Important constraint:

- `source` is single-valued
- a thread cannot honestly present itself as both `vscode` and `cli` at the same time

## Repository Layout

- `codex_sessions.py`
  - CLI entrypoint
- `codex_sessions_web.py`
  - web UI and API server
- `codex_sessions.sh`
  - CLI shell wrapper
- `codex_sessions_web.sh`
  - web shell wrapper
- `test_codex_sessions_web.py`
  - tests for web-side behavior

## Quick Start

Run from the repository root:

```bash
uv run python codex_sessions.py --help
uv run python codex_sessions_web.py --help
```

Typical CLI usage:

```bash
uv run python codex_sessions.py list --limit 20
uv run python codex_sessions.py show <session-id>
uv run python codex_sessions.py set-alias <session-id> <alias>
uv run python codex_sessions.py set-title <session-id> "Clearer title"
uv run python codex_sessions.py set-source <session-id> vscode
uv run python codex_sessions.py set-workdir <session-id> ~/project
uv run python codex_sessions.py resume <session-id>
uv run python codex_sessions.py resume <session-id> --non-interactive --prompt "Please continue pushing toward a verifiable result."
uv run python codex_sessions.py paths
```

Start the web UI:

```bash
uv run python codex_sessions_web.py --host 127.0.0.1 --port 8765
```

Then open:

- `http://127.0.0.1:8765/`
- `http://127.0.0.1:8765/remote`

## Web Routes And APIs

Main routes:

- `GET /`
- `GET /remote`
- `GET /login`

Read APIs:

- `GET /api/sessions`
- `GET /api/history`
- `GET /api/events`
- `GET /api/remote_sessions`
- `GET /api/remote_guard`
- `GET /api/progress`
- `GET /api/targets`

Mutating APIs:

- `POST /api/continue`
- `POST /api/stop`
- `POST /api/set_title`
- `POST /api/clear_title`
- `POST /api/set_source`
- `POST /api/set_workdir`
- `POST /api/archive`
- `POST /api/delete`
- `POST /api/targets`

Compatibility aliases still exist:

- `POST /api/rename` -> `POST /api/set_title`
- `POST /api/unname` -> `POST /api/clear_title`
- `POST /api/set_cwd` -> `POST /api/set_workdir`

## Authentication And Security

The web UI supports optional local password auth.

Behavior:

- direct loopback access (`127.0.0.1` / `localhost`) can be allowed without a password
- proxied / tunneled access requires login when the auth file exists
- unauthenticated API requests return `401`
- mutating API requests require CSRF protection

Generate a random password:

```bash
uv run python codex_sessions_web.py \
  --auth-file ~/.config/codex-sessions-web/auth.json \
  --set-random-password
```

Set a password from stdin:

```bash
printf '%s' 'your-new-password' | \
uv run python codex_sessions_web.py \
  --auth-file ~/.config/codex-sessions-web/auth.json \
  --set-password-stdin
```

Do not commit the auth file. Do not store plaintext passwords in this repository.

## Deployment Notes

Recommended deployment model:

- keep the Python server bound to loopback
- publish it behind a reverse proxy or tunnel if you need remote access
- use the loopback bypass only for direct on-machine use

Example systemd-oriented workflow:

```bash
sudo systemctl status codex-sessions-web.service
sudo systemctl restart codex-sessions-web.service
journalctl -u codex-sessions-web.service -f
```

Exact bind address and port vary by deployment.

## Multi-Machine Use

The built-in target switcher is designed for a small number of explicitly configured hosts.

Model:

- `local` is always present
- ad-hoc target profiles can live in browser `localStorage`
- optional server-managed presets can live in `~/.config/codex-sessions-web/targets.json`
- the control-plane machine talks to the target machine over SSH
- the target machine runs its own loopback-bound `codex_sessions_web.py`

Preferred auth path:

- SSH keys

Supported fallback:

- password auth from the browser modal, stored only in browser session storage

## Development

Basic checks:

```bash
python -m py_compile codex_sessions.py codex_sessions_web.py test_codex_sessions_web.py
python -m unittest test_codex_sessions_web.py
```

## Version Control Notes

Some deployments use colocated `jj + git`, while others are plain runtime directories.

Check what you actually have before assuming workflow:

```bash
jj status
git status
```

## License

Apache-2.0. See [LICENSE](./LICENSE).

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=SpecterHi/codex_manager&type=Date)](https://www.star-history.com/#SpecterHi/codex_manager&Date)
