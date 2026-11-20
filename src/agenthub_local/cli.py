from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from agenthub_local.config import Config


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agenthub-local")
    ap.add_argument("--env", help="settings file (default .agenthub.env, then ~/.agenthub/agenthub.env)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mcp", help="run the MCP server on stdio (for Claude Code / Cursor / Antigravity)")
    d = sub.add_parser("daemon", help="wait for @mentions and notify (no tokens used)")
    d.add_argument("--wake-cmd", help="optional fixed command to start when mentioned (no shell)")
    r = sub.add_parser("router", help="optional: run the Gemini Decision 1 System-1 task router bot")
    r.add_argument("--threshold", type=float, default=0.70, help="confidence threshold for auto-assign (default 0.70)")
    sub.add_parser("init", help="interactive onboarding wizard (env, projects.json & IDE MCP config)")
    sub.add_parser("setup", help="maintainer: create channels, forums, tags and the maintainer role")
    sub.add_parser("doctor", help="check token, server and channels")
    pj = sub.add_parser("project", help="project channels, members and local folder mapping")
    pjs = pj.add_subparsers(dest="pcmd", required=True)
    c = pjs.add_parser("create", help="maintainer: create private #proj-<id> + proj-<id>-tasks")
    c.add_argument("id")
    c.add_argument("--name", default="")
    for name, hlp in (("grant", "maintainer: give a bot or human access"), ("revoke", "maintainer: remove access")):
        g = pjs.add_parser(name, help=hlp)
        g.add_argument("id")
        g.add_argument("member_id", help="Discord user ID of the bot or human")
    pjs.add_parser("list", help="projects on the server and their members")
    mp = pjs.add_parser("map", help="this machine: the folder where this project lives")
    mp.add_argument("id")
    mp.add_argument("folder")
    mp.add_argument("--lock", action="store_true", help="also install the Claude Code guard hook")
    um = pjs.add_parser("unmap", help="this machine: stop accepting work for this project")
    um.add_argument("id")
    lk = pjs.add_parser("lock", help="install the Claude Code guard hook in the project folder")
    lk.add_argument("id")
    gd = sub.add_parser("guard", help="Claude Code PreToolUse hook: block paths outside --root")
    gd.add_argument("--root", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "guard":
        from agenthub_local.scope import guard_decision

        try:
            event = json.load(sys.stdin)
        except json.JSONDecodeError:
            event = {}
        reason = guard_decision(event, args.root)
        if reason:
            print(f"AgentHub project scope: {reason}", file=sys.stderr)
            raise SystemExit(2)  # exit 2 = Claude Code blocks the tool call and shows the reason
        return

    if args.cmd == "project" and args.pcmd in ("map", "unmap", "lock"):
        _project_local(args)
        return

    if args.cmd == "init":
        from agenthub_local.init_wizard import run_init_wizard
        run_init_wizard()
        return

    cfg = Config.from_env(args.env)

    if args.cmd == "mcp":
        from agenthub_local.mcp_server import run
        run(cfg)
    elif args.cmd == "daemon":
        from agenthub_local.daemon import run
        run(cfg, args.wake_cmd)
    elif args.cmd == "router":
        from agenthub_local.router import run_router_daemon
        run_router_daemon(cfg, threshold=args.threshold)
    elif args.cmd == "setup":
        from agenthub_local.discord_rest import DiscordREST
        from agenthub_local.setup import setup_server

        async def go() -> None:
            rest = DiscordREST(cfg.token, cfg.api_base)
            try:
                made = await setup_server(rest, cfg.guild_id, cfg.maintainer_role)
            finally:
                await rest.close()
            print("created: " + (", ".join(made) or "nothing (already set up)"))
        asyncio.run(go())
    elif args.cmd == "project":
        from agenthub_local import setup as st
        from agenthub_local.discord_rest import DiscordREST
        from agenthub_local.scope import check_project_id

        pid = check_project_id(args.id) if getattr(args, "id", None) else ""

        async def proj() -> None:
            rest = DiscordREST(cfg.token, cfg.api_base)
            try:
                if args.pcmd == "create":
                    made = await st.create_project(rest, cfg.guild_id, pid, args.name, cfg.maintainer_role)
                    print("created: " + (", ".join(made) or "nothing (already exists)"))
                    print(f"next: agenthub-local project grant {pid} <bot-or-user-id>")
                elif args.pcmd == "grant":
                    await st.grant_project(rest, cfg.guild_id, pid, args.member_id)
                    print(f"granted {args.member_id} on project {pid}")
                elif args.pcmd == "revoke":
                    await st.revoke_project(rest, cfg.guild_id, pid, args.member_id)
                    print(f"revoked {args.member_id} on project {pid}")
                elif args.pcmd == "list":
                    print(json.dumps(await st.list_projects(rest, cfg.guild_id), indent=1))
            finally:
                await rest.close()
        asyncio.run(proj())
    elif args.cmd == "doctor":
        from agenthub_local.discord_rest import DiscordREST
        from agenthub_local.hub import Hub

        async def check() -> None:
            rest = DiscordREST(cfg.token, cfg.api_base)
            hub = Hub(cfg, rest)
            try:
                me = await hub.me()
                chans = await hub.channels()
                need = [cfg.chat_channel, "agent-roster", cfg.knowledge_forum, cfg.tasks_forum, cfg.debates_forum]
                print(json.dumps({"bot": f"{me['username']} ({me['id']})", "guild": cfg.guild_id,
                                  "missing_channels": [n for n in need if n not in chans]}, indent=1))
            finally:
                await rest.close()
        asyncio.run(check())


if __name__ == "__main__":
    main(sys.argv[1:])


def _project_local(args) -> None:
    """Folder mapping lives only on this machine and is changed only by the human, here."""
    from pathlib import Path

    from agenthub_local.projects import ProjectRegistry
    from agenthub_local.scope import check_project_id, write_hook

    home = Path(os.environ.get("AGENTHUB_HOME", str(Path.home() / ".agenthub")))
    reg = ProjectRegistry(home)
    pid = check_project_id(args.id)
    if args.pcmd == "map":
        folder = Path(args.folder).expanduser().resolve()
        if not folder.is_dir():
            raise SystemExit(f"not a folder: {folder}")
        if folder == Path(folder.anchor) or folder == Path.home():
            raise SystemExit("refusing to map a drive root or your home folder; pick the project folder")
        out = reg.upsert(pid, str(folder), channel_ids=[])
        print(json.dumps(out, indent=1, ensure_ascii=False))
        if args.lock:
            print(f"guard hook: {write_hook(folder)}")
    elif args.pcmd == "unmap":
        print(json.dumps(reg.remove(pid), indent=1))
    elif args.pcmd == "lock":
        local = reg.get(pid)
        if local is None:
            raise SystemExit(f"map it first: agenthub-local project map {pid} <folder>")
        print(f"guard hook: {write_hook(local['path'])}")
