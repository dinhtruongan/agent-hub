"""One-time server setup by a maintainer: channels, forums, tags and the maintainer role."""

from __future__ import annotations

from agenthub_local.discord_rest import DiscordREST
from agenthub_local.hub import KINDS, TASK_STATES

TEXT, FORUM = 0, 15


async def setup_server(rest: DiscordREST, guild_id: str, maintainer_role: str = "maintainer") -> list[str]:
    done: list[str] = []
    existing = {c["name"]: c for c in await rest.guild_channels(guild_id)}
    roles = {r["name"] for r in await rest.guild_roles(guild_id)}
    if maintainer_role not in roles:
        await rest.create_role(guild_id, maintainer_role)
        done.append(f"role @{maintainer_role} (give it to the humans who approve)")
    plan = [
        ("agent-chat", TEXT, None, "Agents and humans talk here. @mention an agent to wake it."),
        ("agent-roster", TEXT, None, "One profile per agent. Written by tools."),
        ("knowledge", FORUM, ["proposed", *KINDS], "Shared knowledge. Maintainers react ✅ verify, ❌ reject, 🗑️ deprecate."),
        ("tasks", FORUM, TASK_STATES, "Tasks. Maintainers react ✅ on a handoff to accept it."),
        ("debates", FORUM, [], "Bounded multi-agent debates. Round 0 is sealed."),
    ]
    for name, typ, tags, topic in plan:
        if name in existing:
            continue
        body: dict = {"name": name, "type": typ, "topic": topic}
        if tags is not None and typ == FORUM:
            body["available_tags"] = [{"name": t} for t in tags][:20]
        await rest.create_channel(guild_id, body)
        done.append(f"#{name}")
    return done


# ------------------------------------------------------------------ projects
async def create_project(rest: DiscordREST, guild_id: str, project_id: str, name: str = "",
                         maintainer_role: str = "maintainer") -> list[str]:
    """Create the private `#proj-<id>` channel and `proj-<id>-tasks` forum for one project.

    `@everyone` is denied VIEW_CHANNEL; the maintainer role and the bot running this command can
    see them. Members are added one by one with `grant_project`.
    """
    from agenthub_local.scope import (
        MEMBER_ALLOW,
        OVERWRITE_MEMBER,
        OVERWRITE_ROLE,
        VIEW_CHANNEL,
        chat_channel_name,
        tasks_forum_name,
    )

    me = await rest.me()
    roles = {r["name"]: r["id"] for r in await rest.guild_roles(guild_id)}
    if maintainer_role not in roles:
        raise SystemExit("maintainer role missing: run `agenthub-local setup` first")
    overwrites = [
        {"id": guild_id, "type": OVERWRITE_ROLE, "allow": "0", "deny": str(VIEW_CHANNEL)},  # @everyone
        {"id": roles[maintainer_role], "type": OVERWRITE_ROLE, "allow": str(MEMBER_ALLOW), "deny": "0"},
        {"id": me["id"], "type": OVERWRITE_MEMBER, "allow": str(MEMBER_ALLOW), "deny": "0"},
    ]
    existing = {c["name"] for c in await rest.guild_channels(guild_id)}
    done: list[str] = []
    label = name or project_id
    for cname, typ, tags, topic in [
        (chat_channel_name(project_id), TEXT, None,
         f"Project {label}. Only members can read. @mention an agent to wake it on its owner's machine."),
        (tasks_forum_name(project_id), FORUM, TASK_STATES,
         f"Tasks of project {label}. Maintainers react ✅ on a handoff to accept it."),
    ]:
        if cname in existing:
            continue
        body: dict = {"name": cname, "type": typ, "topic": topic, "permission_overwrites": overwrites}
        if tags is not None:
            body["available_tags"] = [{"name": t} for t in tags][:20]
        await rest.create_channel(guild_id, body)
        done.append(("#" if typ == TEXT else "forum ") + cname)
    return done


async def _project_channels(rest: DiscordREST, guild_id: str, project_id: str) -> list[dict]:
    from agenthub_local.scope import chat_channel_name, tasks_forum_name

    want = {chat_channel_name(project_id), tasks_forum_name(project_id)}
    chans = [c for c in await rest.guild_channels(guild_id) if c["name"] in want]
    if len(chans) != 2:
        raise SystemExit(f"project '{project_id}' not found: run `agenthub-local project create {project_id}`")
    return chans


async def grant_project(rest: DiscordREST, guild_id: str, project_id: str, member_id: str) -> None:
    """Let one human or bot read and post in the project's channel and task forum."""
    from agenthub_local.scope import MEMBER_ALLOW, OVERWRITE_MEMBER

    for c in await _project_channels(rest, guild_id, project_id):
        await rest.set_permission(c["id"], member_id, MEMBER_ALLOW, 0, OVERWRITE_MEMBER)


async def revoke_project(rest: DiscordREST, guild_id: str, project_id: str, member_id: str) -> None:
    for c in await _project_channels(rest, guild_id, project_id):
        await rest.delete_permission(c["id"], member_id)


async def list_projects(rest: DiscordREST, guild_id: str) -> list[dict]:
    from agenthub_local.scope import TASKS_SUFFIX, members_of, project_of_channel_name

    out = []
    for c in await rest.guild_channels(guild_id):
        pid = project_of_channel_name(c["name"])
        if pid and c.get("type") == TEXT and not c["name"].endswith(TASKS_SUFFIX):
            out.append({"id": pid, "channel": "#" + c["name"], "channel_id": c["id"],
                        "members": sorted(members_of(c))})
    return out
