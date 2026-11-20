"""Project scoping: one Discord channel per project, one local folder per project.

Two independent locks keep an agent inside a project:

1. **Discord (who may talk about the project).** Each project has a private text channel
   `#proj-<id>` and a private forum `proj-<id>-tasks`. `@everyone` cannot see them; only humans
   and bots that a maintainer granted (`agenthub-local project grant`) can read or post. Discord
   enforces this, so a bot outside the project cannot even read the task list.
2. **This machine (which files the agent may touch).** A member maps a project to a folder on
   their own machine (`agenthub-local project map`). While a project is active, every AgentHub
   tool that takes a path (uploads, path checks) only accepts paths inside that folder, and the
   optional Claude Code hook `agenthub-local guard` blocks the IDE's own file tools outside it.
   Only the human edits the mapping; agents cannot widen it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

PROJECT_PREFIX = "proj-"
TASKS_SUFFIX = "-tasks"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")

# Discord permission bits used for project channels.
ADD_REACTIONS = 1 << 6
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
ATTACH_FILES = 1 << 15
READ_MESSAGE_HISTORY = 1 << 16
CREATE_PUBLIC_THREADS = 1 << 35
SEND_MESSAGES_IN_THREADS = 1 << 38
MEMBER_ALLOW = (VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | ATTACH_FILES | READ_MESSAGE_HISTORY
                | ADD_REACTIONS | CREATE_PUBLIC_THREADS | SEND_MESSAGES_IN_THREADS)
OVERWRITE_ROLE, OVERWRITE_MEMBER = 0, 1


def check_project_id(project_id: str) -> str:
    pid = (project_id or "").strip().lower()
    if not _ID_RE.fullmatch(pid) or pid.endswith(TASKS_SUFFIX):
        raise ValueError("project id must be 1-41 chars of a-z, 0-9, '-' or '_' (e.g. 'web', 'video-ai')")
    return pid


def chat_channel_name(project_id: str) -> str:
    return PROJECT_PREFIX + check_project_id(project_id)


def tasks_forum_name(project_id: str) -> str:
    return PROJECT_PREFIX + check_project_id(project_id) + TASKS_SUFFIX


def project_of_channel_name(name: str) -> str | None:
    """`proj-web` -> 'web', `proj-web-tasks` -> 'web', anything else -> None."""
    if not name or not name.startswith(PROJECT_PREFIX):
        return None
    rest = name[len(PROJECT_PREFIX):]
    rest = rest.removesuffix(TASKS_SUFFIX)
    return rest if _ID_RE.fullmatch(rest) else None


def members_of(channel: dict) -> set[str]:
    """User/bot IDs explicitly granted VIEW_CHANNEL on a project channel."""
    out = set()
    for ow in channel.get("permission_overwrites") or []:
        if int(ow.get("type", -1)) == OVERWRITE_MEMBER and int(ow.get("allow", 0)) & VIEW_CHANNEL:
            out.add(str(ow["id"]))
    return out


class PathGuard:
    """Accepts a path only if it resolves inside `root` (symlinks and `..` resolved first)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def contains(self, path: str | Path, base: str | Path | None = None) -> bool:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path(base or self.root) / p
        try:
            return p.resolve().is_relative_to(self.root)
        except (OSError, ValueError):
            return False

    def require(self, path: str | Path, base: str | Path | None = None) -> Path:
        if not self.contains(path, base):
            raise PermissionError(f"path is outside the active project folder {self.root}")
        p = Path(path).expanduser()
        return (p if p.is_absolute() else Path(base or self.root) / p).resolve()


# ------------------------------------------------------------------ Claude Code hook (guard)
_PATH_KEYS = ("file_path", "path", "notebook_path")
_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def _looks_like_path(tok: str) -> bool:
    return (tok.startswith(("/", "~", "..", "\\\\")) or bool(_WIN_ABS.match(tok))
            or "/../" in tok or "\\..\\" in tok)


def guard_decision(event: dict, root: str | Path) -> str | None:
    """Return a reason to block a Claude Code tool call, or None to allow it.

    File tools are checked exactly. For Bash this is a best-effort check of path-like tokens
    (absolute paths, `~`, `..`); it is a seatbelt, not a sandbox.
    """
    guard = PathGuard(root)
    tool = event.get("tool_name", "")
    tin = event.get("tool_input") or {}
    cwd = event.get("cwd") or str(guard.root)
    if not guard.contains(cwd):
        return f"working directory {cwd} is outside project folder {guard.root}"
    for k in _PATH_KEYS:
        v = tin.get(k)
        if isinstance(v, str) and v and not guard.contains(v, cwd):
            return f"{tool}: {v} is outside project folder {guard.root}"
    pat = tin.get("pattern")
    if tool == "Glob" and isinstance(pat, str) and _looks_like_path(pat) and not guard.contains(pat, cwd):
        return f"Glob: {pat} is outside project folder {guard.root}"
    if tool == "Bash":
        cmd = str(tin.get("command", ""))
        try:
            toks = shlex.split(cmd, posix=os.name != "nt")
        except ValueError:
            toks = cmd.split()
        for tok in toks:
            for part in [tok, *tok.split("=")[1:]]:
                part = part.strip("'\"")
                if part and _looks_like_path(part) and not guard.contains(part, cwd):
                    return f"Bash: {part} is outside project folder {guard.root}"
    return None


def hook_settings(root: str | Path, exe: str = "agenthub-local") -> dict:
    """`.claude/settings.local.json` content that installs the guard as a PreToolUse hook."""
    r = str(Path(root).resolve())
    q = '"' + r.replace('"', "") + '"'
    return {"hooks": {"PreToolUse": [{
        "matcher": "Read|Write|Edit|MultiEdit|Glob|Grep|NotebookEdit|Bash",
        "hooks": [{"type": "command", "command": f"{exe} guard --root {q}"}],
    }]}}


def write_hook(root: str | Path, exe: str = "agenthub-local") -> Path:
    """Merge the guard hook into <root>/.claude/settings.local.json (kept out of git by Claude Code)."""
    path = Path(root).resolve() / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    new = hook_settings(root, exe)["hooks"]["PreToolUse"][0]
    pre = [h for h in data.setdefault("hooks", {}).setdefault("PreToolUse", [])
           if "agenthub-local guard" not in json.dumps(h) and f"{exe} guard" not in json.dumps(h)]
    pre.append(new)
    data["hooks"]["PreToolUse"] = pre
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path
