"""Project scoping: private project channels, folder confinement, cross-machine task assignment."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agenthub_local.config import Config
from agenthub_local.daemon import workspace_for
from agenthub_local.discord_rest import DiscordError, DiscordREST
from agenthub_local.hub import VERIFY, Hub, HubError
from agenthub_local.inbox import Inbox
from agenthub_local.mcp_server import build
from agenthub_local.projects import ProjectRegistry
from agenthub_local.scope import (
    PathGuard,
    check_project_id,
    guard_decision,
    project_of_channel_name,
    write_hook,
)
from agenthub_local.setup import (
    create_project,
    grant_project,
    list_projects,
    revoke_project,
    setup_server,
)
from tests.local.fakediscord import FakeDiscord


class World:
    """Two members (An, Giang) on two machines, plus an outsider bot (Minh) not in the project."""

    def __init__(self, tmp: Path) -> None:
        self.d = FakeDiscord()
        self.tmp = tmp
        self.admin = self.d.add_user("t-admin", "admin-bot", bot=True)
        self.lead = self.d.add_user("t-lead", "lead", bot=False)
        self.bots = {n: self.d.add_user(f"t-{n}", n, bot=True) for n in ("an", "giang", "minh")}
        self.admin_rest = DiscordREST("t-admin", transport=self.d.transport("t-admin"))

    async def setup(self) -> None:
        await setup_server(self.admin_rest, self.d.guild_id)
        self.d.give_role(self.lead, "maintainer")
        await create_project(self.admin_rest, self.d.guild_id, "web", "Web App")
        for n in ("an", "giang"):
            await grant_project(self.admin_rest, self.d.guild_id, "web", self.bots[n]["id"])

    def hub(self, name: str, mapped: bool = True) -> Hub:
        home = self.tmp / name / ".agenthub"
        folder = self.tmp / name / "web"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "app.py").write_text("print('hi')\n")
        if mapped:
            ProjectRegistry(home).upsert("web", str(folder))
        cfg = Config(token=f"t-{name}", guild_id=self.d.guild_id, owner_id=self.lead["id"], home=home)
        return Hub(cfg, DiscordREST(cfg.token, transport=self.d.transport(cfg.token)))


@pytest.fixture
async def world(tmp_path) -> World:
    w = World(tmp_path)
    await w.setup()
    return w


def test_project_ids_and_channel_names():
    assert check_project_id(" Web ") == "web"
    for bad in ("", "../x", "a b", "x-tasks", "a" * 50):
        with pytest.raises(ValueError):
            check_project_id(bad)
    assert project_of_channel_name("proj-web") == "web"
    assert project_of_channel_name("proj-web-tasks") == "web"
    assert project_of_channel_name("agent-chat") is None


async def test_project_channels_are_private(world):
    chans = {c["name"]: c for c in world.d.channels.values()}
    assert {"proj-web", "proj-web-tasks"} <= set(chans)
    minh = world.d.users["t-minh"]
    assert not world.d.can_view(minh, chans["proj-web"]["id"])
    assert world.d.can_view(world.bots["an"], chans["proj-web"]["id"])
    assert world.d.can_view(world.lead, chans["proj-web"]["id"])  # maintainer role
    listed = await list_projects(world.admin_rest, world.d.guild_id)
    assert listed[0]["id"] == "web" and world.bots["giang"]["id"] in listed[0]["members"]


async def test_use_project_requires_mapping_and_membership(world):
    with pytest.raises(HubError, match="not mapped"):
        await world.hub("an", mapped=False).use_project("web")
    with pytest.raises(HubError, match="not a member"):
        await world.hub("minh").use_project("web")
    info = await world.hub("an").use_project("web")
    assert info["active"] == "web" and info["folder"].endswith(str(Path("an") / "web"))
    assert {m["id"] for m in info["members"]} >= {world.bots["an"]["id"], world.bots["giang"]["id"]}
    # revoking access locks the agent out again
    await revoke_project(world.admin_rest, world.d.guild_id, "web", world.bots["an"]["id"])
    with pytest.raises(HubError, match="not a member"):
        await world.hub("an").use_project("web")


async def test_folder_confinement(world, tmp_path):
    an = world.hub("an")
    other = tmp_path / "an" / "secret.txt"
    other.write_text("x")
    # another project mapped on the same machine is NOT reachable while 'web' is active
    elsewhere = tmp_path / "an" / "other-proj"
    elsewhere.mkdir()
    (elsewhere / "f.txt").write_text("x")
    ProjectRegistry(an.cfg.home).upsert("other", str(elsewhere))
    await an.use_project("web")
    root = Path(an.project["root"])
    assert an.check_path(str(root / "app.py"))["allowed"]
    for bad in (str(other), str(root / ".." / "secret.txt"), str(elsewhere / "f.txt")):
        assert not an.check_path(bad)["allowed"]
        with pytest.raises(HubError):
            await an.send("file", file=bad)
    assert (await an.send("ok", file=str(root / "app.py")))["message_id"]


async def test_cross_machine_assignment_flow(world):
    an, giang = world.hub("an"), world.hub("giang")
    await an.use_project("web")
    await giang.use_project("web")
    gid = world.bots["giang"]["id"]

    # cannot assign agents outside the project
    with pytest.raises(HubError, match="not members"):
        await an.create_task("x", "y", ["z"], assignees=[world.bots["minh"]["id"]])
    with pytest.raises(HubError, match="not members"):
        await an.send("hi", to_bot_ids=[world.bots["minh"]["id"]])

    t = await an.create_task("Fix login", "Login fails on Safari", ["test passes"], assignees=[gid])
    assert t["project"] == "web"
    chat_id = an.project["chat_id"]
    ping = world.d.messages[chat_id][-1]
    assert any(u["id"] == gid for u in ping["mentions"])  # Giang's daemon wakes on this

    # Giang's daemon maps the mention to Giang's own folder for 'web'
    reg_g = ProjectRegistry(giang.cfg.home)
    ws = workspace_for(reg_g, chat_id, "proj-web", "", ping["content"])
    assert ws["scoped"] and ws["workspace_path"] == giang.project["root"]
    # a machine that never mapped the project refuses
    assert workspace_for(ProjectRegistry(world.hub("minh", mapped=False).cfg.home),
                         chat_id, "proj-web", "", "") is None

    # only the assignee can claim, and only from inside the project
    with pytest.raises(HubError, match="assigned to other"):
        await an.claim(t["id"])
    unscoped = world.hub("giang")
    with pytest.raises(HubError, match="hub_project_use"):
        await unscoped.claim(t["id"])
    assert (await giang.claim(t["id"]))["claimed_by"] == gid
    ctx = await giang.context()
    await giang.handoff(t["id"], "fixed", ctx["context_version"], [], ["https://ci/run/1"])
    task = await giang.task(t["id"])
    assert task["state"] == "review"
    handoff = task["handoff"]["message_id"]
    world.d.react_as(world.lead, t["id"], handoff, VERIFY)
    assert (await an.task(t["id"]))["state"] == "done"

    # outsiders cannot even read the project's tasks
    minh = world.hub("minh")
    with pytest.raises(DiscordError):
        await minh.task(t["id"])


async def test_mcp_pinned_project_and_mapping_lock(world):
    an = world.hub("an")
    server = build(an, Inbox(an.cfg.home), pinned_project="web")
    names = {t.name for t in await server.list_tools()}
    assert {"hub_project_use", "hub_project_info", "hub_check_path"} <= names

    def text(res) -> str:
        items = getattr(res, "content", res[0] if isinstance(res, tuple) else res)
        return json.dumps([getattr(c, "text", "") for c in items])

    assert "web" in text(await server.call_tool("hub_project_info", {}))
    assert "pinned" in text(await server.call_tool("hub_project_use", {"project_id": "other"}))
    out = text(await server.call_tool("hub_projects", {"action": "upsert", "project_id": "x", "path": "/"}))
    assert "only the human owner" in out


def test_guard_hook(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    ev = lambda tool, **ti: {"tool_name": tool, "tool_input": ti, "cwd": str(root)}
    assert guard_decision(ev("Read", file_path=str(root / "src" / "a.py")), root) is None
    assert guard_decision(ev("Edit", file_path="src/a.py"), root) is None
    assert guard_decision(ev("Read", file_path=str(tmp_path / "x")), root)
    assert guard_decision(ev("Write", file_path="../escape.txt"), root)
    assert guard_decision(ev("Grep", pattern="x", path="/etc"), root)
    assert guard_decision(ev("Glob", pattern="/etc/*"), root)
    assert guard_decision(ev("Bash", command="cat /etc/passwd"), root)
    assert guard_decision(ev("Bash", command="cp a.txt ../../x"), root)
    assert guard_decision(ev("Bash", command="ls ~"), root)
    assert guard_decision(ev("Bash", command="pytest -q src"), root) is None
    assert guard_decision({"tool_name": "Read", "tool_input": {"file_path": "a"}, "cwd": str(tmp_path)}, root)
    assert PathGuard(root).contains(root / "src")

    hook = write_hook(root)
    data = json.loads(hook.read_text())
    cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "agenthub-local guard --root" in cmd
    write_hook(root)  # idempotent
    assert len(json.loads(hook.read_text())["hooks"]["PreToolUse"]) == 1

    # the CLI really exits 2 (block) / 0 (allow)
    def run(event):
        return subprocess.run([sys.executable, "-m", "agenthub_local.cli", "guard", "--root", str(root)],
                              input=json.dumps(event), text=True, capture_output=True, check=False)
    blocked = run(ev("Read", file_path="/etc/passwd"))
    assert blocked.returncode == 2 and "outside project folder" in blocked.stderr
    assert run(ev("Read", file_path="src/a.py")).returncode == 0
