"""Serverless hub on Discord: tests run against an in-memory Discord API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthub_local.config import Config
from agenthub_local.daemon import WakeFilter
from agenthub_local.discord_rest import DiscordREST
from agenthub_local.hub import DEPRECATE, REJECT, VERIFY, Hub, HubError
from agenthub_local.inbox import Inbox
from agenthub_local.notify import build_command
from agenthub_local.setup import setup_server
from tests.local.fakediscord import FakeDiscord


class Team:
    def __init__(self, tmp: Path) -> None:
        self.d = FakeDiscord()
        self.admin = self.d.add_user("t-admin", "admin-bot", bot=True)
        self.lead = self.d.add_user("t-lead", "lead-human", bot=False)       # maintainer
        self.dev = self.d.add_user("t-dev", "dev-human", bot=False)          # not maintainer
        self.bots = {n: self.d.add_user(f"t-{n}", n, bot=True) for n in ("a1", "a2", "b1", "c1")}
        self.tmp = tmp
        self.hubs: dict[str, Hub] = {}

    async def setup(self) -> None:
        rest = DiscordREST("t-admin", transport=self.d.transport("t-admin"))
        await setup_server(rest, self.d.guild_id)
        self.d.give_role(self.lead, "maintainer")

    def hub(self, name: str, clock=None) -> Hub:
        cfg = Config(token=f"t-{name}", guild_id=self.d.guild_id, owner_id=self.lead["id"],
                     home=self.tmp / name, upload_root=self.tmp / "proj")
        h = Hub(cfg, DiscordREST(cfg.token, transport=self.d.transport(cfg.token)),
                **({"clock": clock} if clock else {}))
        self.hubs[name] = h
        return h

    def react(self, user: dict, channel_id: str, message_id: str, emoji: str) -> None:
        self.d.react_as(user, channel_id, message_id, emoji)


@pytest.fixture
async def team(tmp_path) -> Team:
    t = Team(tmp_path)
    await t.setup()
    return t


async def test_setup_is_idempotent(team):
    rest = DiscordREST("t-admin", transport=team.d.transport("t-admin"))
    assert await setup_server(rest, team.d.guild_id) == []
    names = {c["name"] for c in team.d.channels.values()}
    assert {"agent-chat", "agent-roster", "knowledge", "tasks", "debates"} <= names


async def test_only_human_maintainers_verify(team):
    a1, b1 = team.hub("a1"), team.hub("b1")
    rec = await a1.propose("requirement", "Auth", "Use Discord identities")
    rid = rec["id"]
    assert (await b1.records())[0].status == "proposed"
    team.react(team.bots["b1"], rid, rid, VERIFY)          # another agent: ignored
    team.react(team.dev, rid, rid, VERIFY)                 # human without the role: ignored
    assert (await b1.records())[0].status == "proposed"
    v0 = (await b1.context())["context_version"]
    team.react(team.lead, rid, rid, VERIFY)
    b1._member_cache.clear()
    recs = await b1.records()
    assert recs[0].status == "verified" and recs[0].verified_by == [team.lead["id"]]
    assert (await b1.context())["context_version"] != v0


async def test_supersede_and_drift(team):
    a1, a2 = team.hub("a1"), team.hub("a2")
    old = (await a1.propose("architecture", "Store", "Use Postgres"))["id"]
    team.react(team.lead, old, old, VERIFY)
    ctx1 = await a2.context()
    assert [r["id"] for r in ctx1["verified"]] == [old]
    new = (await a1.propose("architecture", "Store", "Use Discord forums", supersedes=old))["id"]
    team.react(team.lead, new, new, VERIFY)
    rep = await a2.drift(ctx1["context_version"], [old])
    assert rep["stale"]
    assert rep["outdated"][0]["status"] == "superseded"
    assert rep["outdated"][0]["replacement"]["id"] == new
    assert [r["id"] for r in rep["added"]] == [new] and rep["removed"] == [old]
    ctx2 = await a2.context()
    assert [r["id"] for r in ctx2["verified"]] == [new]
    assert not (await a2.drift(ctx2["context_version"], [new]))["stale"]
    # deprecate / reject also take effect only from maintainers
    team.react(team.lead, new, new, DEPRECATE)
    assert (await a2.drift(ctx2["context_version"], [new]))["outdated"][0]["status"] == "deprecated"
    prop = (await a1.propose("fact", "x", "y"))["id"]
    team.react(team.lead, prop, prop, REJECT)
    assert next(r for r in await a2.records() if r.id == prop).status == "rejected"


async def test_task_claim_race_handoff_and_human_approval(team):
    a1, b1 = team.hub("a1"), team.hub("b1")
    t = await a1.create_task("Build login", "Discord OAuth", ["works", "tests pass"])
    tid = t["id"]
    await a1.claim(tid)
    with pytest.raises(HubError, match="in_progress"):
        await b1.claim(tid)
    ctx = await a1.context(tid)
    with pytest.raises(HubError, match="only the agent that claimed"):
        await b1.handoff(tid, "done", ctx["context_version"], [], ["ci"])
    out = await a1.handoff(tid, "Login done", ctx["context_version"], [], [])
    assert out["state"] == "review"
    task = await b1.task(tid)
    hid = task["handoff"]["message_id"]
    handoff_msg = next(m for m in team.d.messages[tid] if m["id"] == hid)
    assert "No evidence" in handoff_msg["content"]
    team.react(team.bots["b1"], tid, hid, VERIFY)      # agents cannot approve
    assert (await b1.task(tid))["state"] == "review"
    team.react(team.lead, tid, hid, VERIFY)
    assert (await b1.task(tid))["state"] == "done"


async def test_claim_race_earliest_wins(team):
    a1 = team.hub("a1")
    tid = (await a1.create_task("t", "o", []))["id"]
    # Simulate b1 posting its claim message first, then a1 trying to claim.
    team.d._msg(tid, team.bots["b1"], {"content": "claim", "embeds": [{"footer": {"text": 'ah {"t": "claim"}'}}]})
    with pytest.raises(HubError):
        await a1.claim(tid)
    assert (await a1.task(tid))["claimed_by"] == team.bots["b1"]["id"]


async def test_debate_sealed_round_and_bounds(team):
    now = [1_000_000.0]
    hubs = {n: team.hub(n, clock=lambda: now[0]) for n in ("a1", "a2", "b1", "c1")}
    ids = {n: team.bots[n]["id"] for n in hubs}
    parts = {ids["a1"]: "proposer", ids["b1"]: "critic", ids["c1"]: "synthesizer"}
    with pytest.raises(HubError):
        await hubs["a1"].start_debate("Q?", {ids["a1"]: "proposer", ids["b1"]: "critic"})
    did = (await hubs["a2"].start_debate("Which store?", parts, rounds=2))["id"]

    await hubs["a1"].debate_submit(did, "Forums")
    view_b = await hubs["b1"].debate_read(did)
    assert view_b["sealed_round0"] and view_b["turns"] == []          # b1 cannot see a1 yet
    with pytest.raises(HubError, match="round 0 is not complete"):
        await hubs["a1"].debate_submit(did, "rebuttal too early")
    with pytest.raises(HubError, match="wait until round 1"):
        await hubs["c1"].debate_submit(did, "synth too early")
    with pytest.raises(HubError, match="not a participant"):
        await hubs["a2"].debate_submit(did, "x")
    await hubs["b1"].debate_submit(did, "Postgres")
    view_b = await hubs["b1"].debate_read(did)
    assert not view_b["sealed_round0"] and len(view_b["turns"]) == 2
    assert all(t["content"].startswith("<untrusted") for t in view_b["turns"])
    await hubs["a1"].debate_submit(did, "r1 a1")
    await hubs["b1"].debate_submit(did, "r1 b1")
    with pytest.raises(HubError, match="all rounds"):
        await hubs["a1"].debate_submit(did, "r2")
    out = await hubs["c1"].debate_submit(did, "Agree: forums; disagree: scale")
    assert out["posted"] == "synthesis"
    with pytest.raises(HubError, match="already"):
        await hubs["c1"].debate_submit(did, "again")


async def test_debate_deadline_unblocks(team):
    now = [1_000_000.0]
    hubs = {n: team.hub(n, clock=lambda: now[0]) for n in ("a1", "b1", "c1")}
    ids = {n: team.bots[n]["id"] for n in hubs}
    did = (await hubs["a1"].start_debate("Q", {ids["a1"]: "proposer", ids["b1"]: "critic",
                                                ids["c1"]: "synthesizer"}, rounds=1, deadline_minutes=5))["id"]
    await hubs["a1"].debate_submit(did, "only me")       # b1 never answers (unavailable)
    now[0] += 600
    assert (await hubs["c1"].debate_submit(did, "summary with b1 missing"))["posted"] == "synthesis"


async def test_send_hop_limit_and_upload_guard(team, tmp_path):
    a1, b1 = team.hub("a1"), team.hub("b1")
    m = await a1.send("hello", [team.bots["b1"]["id"]])
    sent = next(x for x in team.d.messages.values() for x in x if x["id"] == m["message_id"])
    assert sent["mentions"][0]["id"] == team.bots["b1"]["id"]
    parent = m["message_id"]
    for i in range(a1.cfg.max_hops):
        parent = (await (b1 if i % 2 == 0 else a1).send(f"r{i}", reply_to=parent))["message_id"]
    with pytest.raises(HubError, match="hop limit"):
        await b1.send("one more", reply_to=parent)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "ok.txt").write_text("hi")
    (proj / ".env").write_text("SECRET=1")
    (tmp_path / "outside.txt").write_text("no")
    assert (await a1.send("file", file=str(proj / "ok.txt")))["message_id"]
    for bad in (proj / ".env", tmp_path / "outside.txt", Path.home() / ".ssh" / "id_rsa"):
        with pytest.raises(HubError):
            await a1.send("file", file=str(bad))


async def test_fallback_inbox_scans_mentions(team):
    a1, b1 = team.hub("a1"), team.hub("b1")
    await a1.send("ping b1", [team.bots["b1"]["id"]])
    await a1.send("not for b1")
    found = await b1.mentions_since(None)
    assert [f["content"].split()[-1] for f in found] == ["b1"]


def test_notify_never_embeds_text_in_script():
    evil = '$(Remove-Item C:\\ -Recurse) "@ ; rm -rf ~ `whoami`'
    cmd, env = build_command("t", evil, platform="win32")
    assert all(evil not in part for part in cmd)
    assert env["AH_BODY"] == evil
    cmd, _ = build_command("t", evil, platform="darwin")
    assert cmd[-1] == evil and evil not in cmd[2]   # passed as argv, script is constant


def test_wake_filter_limits_bots_not_humans():
    now = [0.0]
    f = WakeFilter(max_hops=3, clock=lambda: now[0])
    assert f.allow("human", False, 99)
    assert not f.allow("bot", True, 3)
    assert all(f.allow("bot", True, 0) for _ in range(5))
    assert not f.allow("bot", True, 0)
    now[0] += 601
    assert f.allow("bot", True, 0)


def test_inbox_unread_and_cursor(tmp_path):
    ib = Inbox(tmp_path)
    assert ib.unread() == []
    ib.append({"id": "5", "content": "a"})
    ib.append({"id": "7", "content": "b"})
    assert [i["id"] for i in ib.unread()] == ["5", "7"]
    assert ib.unread() == []
    ib.append({"id": "9", "content": "c"})
    assert [i["id"] for i in ib.wait(1, poll=0.01)] == ["9"]


async def test_mcp_tools_exposed_and_callable(team):
    from agenthub_local.mcp_server import build

    a1 = team.hub("a1")
    server = build(a1, Inbox(a1.cfg.home))
    names = {t.name for t in await server.list_tools()}
    assert {"hub_context", "hub_propose", "hub_handoff", "hub_debate_submit", "hub_wait", "hub_inbox"} <= names
    await server.call_tool("hub_propose", {"kind": "fact", "title": "T", "body": "B"})
    res = await server.call_tool("hub_context", {})
    items = getattr(res, "content", res[0] if isinstance(res, tuple) else res)
    text = json.dumps([getattr(c, "text", "") for c in items])
    assert "context_version" in text and "Proposed, NOT verified" in text
    bad = await server.call_tool("hub_propose", {"kind": "nope", "title": "T", "body": "B"})
    bad_items = getattr(bad, "content", bad[0] if isinstance(bad, tuple) else bad)
    assert "error" in json.dumps([getattr(c, "text", "") for c in bad_items])
