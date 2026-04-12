#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List


@dataclass(frozen=True)
class PatchRule:
    description: str
    before: str
    after: str


PATCH_RULES: tuple[PatchRule, ...] = (
    PatchRule(
        description="Prefer live preview over cached title for open tabs (0.4.x)",
        before="l=s??c??Q0",
        after="l=c??s??Q0",
    ),
    PatchRule(
        description="Prefer live preview over cached title for open tabs (0.4.78)",
        before="l=s??c??X0",
        after="l=c??s??X0",
    ),
    PatchRule(
        description="Prefer live preview over cached title for open tabs (26.x)",
        before="l=s??c??x0",
        after="l=c??s??x0",
    ),
    PatchRule(
        description="Expose thread/list name field to legacy preview loader (0.4.x)",
        before="let u=a.result.data.map(l=>({conversationId:l.id,preview:l.preview}));i(u)",
        after="let u=a.result.data.map(l=>({conversationId:l.id,preview:l.preview,name:l.name}));i(u)",
    ),
    PatchRule(
        description="Prefer thread/list name, then preview, then cache in preview loader (0.4.x)",
        before="let i=r.titles[o.conversationId];n.set(String(o.conversationId),i??o.preview)",
        after='let i=(o.name??"").trim(),s=r.titles[o.conversationId];n.set(String(o.conversationId),i||o.preview||s)',
    ),
    PatchRule(
        description="Prefer preview over cache in preview loader (26.x)",
        before="n.set(String(o.id),i||s||o.preview)",
        after="n.set(String(o.id),i||o.preview||s)",
    ),
    PatchRule(
        description="Prefer thread/list name, then preview, then cache in chat session list (0.4.x)",
        before="return{conversationId:e.id,preview:r??e.preview,createdAtMs:o,modelProvider:e.modelProvider}",
        after='let i=e.name?.trim()??"";return{conversationId:e.id,preview:i||e.preview||r,createdAtMs:o,modelProvider:e.modelProvider}',
    ),
    PatchRule(
        description="Prefer thread/list name, then preview, then cache in chat session list (26.x)",
        before='return{conversationId:e.id,preview:i||r||e.preview,createdAtMs:o,modelProvider:e.modelProvider}',
        after='return{conversationId:e.id,preview:i||e.preview||r,createdAtMs:o,modelProvider:e.modelProvider}',
    ),
    PatchRule(
        description="Export current VSCode task list previews for web app sync (26.x)",
        before='provideChatSessionItems(e,r,n){return(await this.requestThreadList(e)).data.map(i=>{let s=this.toThreadListSummary(i,n.titles[i.id]);return{summary:s,item:this.toChatSessionItem(s)}})}',
        after='provideChatSessionItems(e,r,n){let o=(await this.requestThreadList(e)).data.map(i=>{let s=this.toThreadListSummary(i,n.titles[i.id]);return{summary:s,item:this.toChatSessionItem(s)}});try{rw.writeFileSync(`${process.env.HOME??"/home/dell"}/.codex/vscode_task_list.json`,JSON.stringify(o.map(i=>i.summary)),{encoding:"utf8"})}catch{}return o}',
    ),
)


PROCESS_PATTERNS: tuple[str, ...] = (
    "bootstrap-fork --type=extensionHost",
    r"openai.chatgpt-.*/bin/.*/codex app-server",
)


def iter_extension_files(root: Path) -> Iterable[Path]:
    yield from sorted(root.glob("openai.chatgpt-*-linux-x64/out/extension.js"))


def collect_pids(pattern: str) -> list[int]:
    completed = subprocess.run(
        ["pgrep", "-f", pattern],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError(completed.stderr.strip() or f"pgrep failed for pattern: {pattern}")
    pids: list[int] = []
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def restart_remote_extension_processes() -> None:
    all_pids: set[int] = set()
    for pattern in PROCESS_PATTERNS:
        all_pids.update(collect_pids(pattern))
    if not all_pids:
        print("No remote extensionHost / codex app-server processes found.")
        return

    ordered = sorted(all_pids)
    subprocess.run(
        ["kill", "-TERM", *[str(pid) for pid in ordered]],
        check=False,
    )
    time.sleep(3.0)
    print("Restarted remote extension processes:", " ".join(str(pid) for pid in ordered))


def patch_file(path: Path, *, dry_run: bool) -> tuple[str, list[str]]:
    original = path.read_text(encoding="utf-8")
    updated = original
    notes: list[str] = []
    changed = False

    for rule in PATCH_RULES:
        before_count = updated.count(rule.before)
        if before_count > 0:
            updated = updated.replace(rule.before, rule.after)
            changed = True
            notes.append(f"applied: {rule.description} x{before_count}")

    if changed and not dry_run:
        path.write_text(updated, encoding="utf-8")
    if changed:
        return ("would-patch" if dry_run else "patched"), notes
    if any(rule.after in updated for rule in PATCH_RULES):
        return "already-patched", []
    return "no-known-patterns", []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch installed remote VSCode Codex extensions to prefer live thread titles."
    )
    parser.add_argument(
        "--extensions-root",
        type=Path,
        default=Path.home() / ".vscode-server" / "extensions",
        help="VSCode server extensions root",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report patch status without writing files",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Restart remote extensionHost / codex app-server after patching",
    )
    args = parser.parse_args()

    files = list(iter_extension_files(args.extensions_root))
    if not files:
        print(f"No extension bundles found under {args.extensions_root}")
        return 1

    modified = 0
    for path in files:
        status, notes = patch_file(path, dry_run=args.dry_run)
        if status in {"patched", "would-patch"}:
            modified += 1
        print(f"[{status}] {path}")
        for note in notes:
            print(f"  - {note}")

    if args.restart and not args.dry_run:
        restart_remote_extension_processes()

    print(
        f"Summary: files={len(files)} modified={modified} dry_run={'yes' if args.dry_run else 'no'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
