"""Tests for Gemini Decision 1 TaskRouter, multi-project workspace registry, and onboarding."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agenthub_local.config import Config
from agenthub_local.discord_rest import DiscordREST
from agenthub_local.hub import Hub, HubError
from agenthub_local.inbox import Inbox
from agenthub_local.init_wizard import write_antigravity_mcp_config
from agenthub_local.mcp_server import build
from agenthub_local.projects import ProjectRegistry
from agenthub_local.router import TaskRouter
from agenthub_local.setup import setup_server
from tests.local.fakediscord import FakeDiscord


class FakeDecisionBackend:
    """Deterministic offline System-1 decision backend for unit tests."""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def decide(
        self,
        prompt: str,
        questions: dict[str, dict[str, Any]],
        media: dict[str, Path | None] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "questions": questions, "media": media})
        new_msg = prompt.split("NEW HUMAN MESSAGE", 1)[-1]
        for needle, resp in self.responses.items():
            if needle in new_msg:
                return resp
        return {
            "is_actionable_task": {"noul": 0.05, "confidence": 0.95},
            "assignee": {"choice": "NONE", "confidence": 0.99, "probabilities": {"NONE": 0.99}},
            "urgency": {"score": 0.0, "confidence": 0.95},
        }


async def _setup_hub_team(tmp_path: Path):
    d = FakeDiscord()
    admin = d.add_user("t-admin", "admin-bot", bot=True)
    lead = d.add_user("t-lead", "lead-human", bot=False)
    router_bot = d.add_user("t-router", "router-bot", bot=True)
    script_bot = d.add_user("t-script", "ai-script", bot=True)
    video_bot = d.add_user("t-video", "ai-video", bot=True)

    rest_admin = DiscordREST("t-admin", transport=d.transport("t-admin"))
    await setup_server(rest_admin, d.guild_id)
    d.give_role(lead, "maintainer")

    def make_hub(token: str, home_name: str, upload_root: Path | None = None) -> Hub:
        cfg = Config(
            token=token,
            guild_id=d.guild_id,
            owner_id=lead["id"],
            home=tmp_path / home_name,
            upload_root=upload_root,
        )
        return Hub(cfg, DiscordREST(token, transport=d.transport(token)))

    h_script = make_hub("t-script", "script")
    h_video = make_hub("t-video", "video")
    h_router = make_hub("t-router", "router")

    await h_script.register("AI-Script", "gemini-4", "scriptwriting, hooks, fact-checking")
    await h_video.register("AI-Video", "gemini-4", "video rendering, CapCut editing, YouTube uploads")
    return d, lead, router_bot, script_bot, video_bot, h_router, h_script, h_video


async def test_router_assign_clarify_and_ignore(tmp_path):
    d, lead, router_bot, script_bot, video_bot, h_router, _, _ = await _setup_hub_team(tmp_path)
    sid, vid = script_bot["id"], video_bot["id"]

    backend = FakeDecisionBackend(
        {
            "Chernobyl hook": {
                "is_actionable_task": {"noul": 0.97, "confidence": 0.97},
                "assignee": {"choice": sid, "confidence": 0.94, "probabilities": {sid: 0.94, vid: 0.04}},
                "urgency": {"score": 2.4, "confidence": 0.85},
            },
            "fix both script and render": {
                "is_actionable_task": {"noul": 0.92, "confidence": 0.92},
                "assignee": {"choice": vid, "confidence": 0.52, "probabilities": {vid: 0.52, sid: 0.44}},
                "urgency": {"score": 3.0, "confidence": 0.80},
            },
        }
    )
    router = TaskRouter(h_router, backend=backend, threshold=0.70, router_bot_id=router_bot["id"])

    chat = next(c for c in d.channels.values() if c["name"] == "agent-chat")
    m1 = d._msg(chat["id"], lead, {"content": "Write a 15s Chernobyl hook"})
    r1 = await router.dispatch_if_needed(m1["id"], "lead-human", m1["content"])
    assert r1.decision == "ASSIGN" and r1.bot_id == sid and r1.agent_name == "AI-Script"
    posted = d.messages[chat["id"]][-1]
    assert posted["mentions"][0]["id"] == sid
    assert "AI-Script" in posted["content"]

    m2 = d._msg(chat["id"], lead, {"content": "Please fix both script and render"})
    r2 = await router.dispatch_if_needed(m2["id"], "lead-human", m2["content"])
    assert r2.decision == "CLARIFY"
    clarify_msg = d.messages[chat["id"]][-1]
    assert clarify_msg["mentions"] == []  # clarify must NOT wake bots
    assert "**AI-Video**" in clarify_msg["content"] and "**AI-Script**" in clarify_msg["content"]

    before_count = len(d.messages[chat["id"]])
    m3 = d._msg(chat["id"], lead, {"content": "haha lunch time?"})
    r3 = await router.dispatch_if_needed(m3["id"], "lead-human", m3["content"])
    assert r3.decision == "IGNORE"
    assert len(d.messages[chat["id"]]) == before_count + 1  # router stayed silent


async def test_multi_project_resolution_and_upload_containment(tmp_path):
    d, lead, _, _, _, _, h_script, _ = await _setup_hub_team(tmp_path)
    p_yt = tmp_path / "proj_youtube"
    p_web = tmp_path / "proj_web"
    p_outside = tmp_path / "outside"
    for p in (p_yt, p_web, p_outside):
        p.mkdir()
    (p_yt / "ok.txt").write_text("yt")
    (p_web / "index.ts").write_text("web")
    (p_web / ".env").write_text("SECRET=1")
    (p_outside / "nope.txt").write_text("no")

    reg = ProjectRegistry(h_script.cfg.home)
    reg.upsert("youtube", str(p_yt), name="YouTube Doc", channel_ids=["111"], aliases=["chernobyl", "shorts"])
    reg.upsert("web", str(p_web), name="Web App", channel_ids=["222"], aliases=["frontend", "api"])

    # Priority: explicit tag > alias > channel > default
    assert reg.resolve("111", "fix #web login")["project_id"] == "web"
    assert reg.resolve("999", "edit chernobyl episode")["project_id"] == "youtube"
    assert reg.resolve("222", "general status check")["project_id"] == "web"
    assert reg.resolve("999", "unrelated")["workspace_path"] == str(p_yt.resolve())

    # Hub._safe_upload allows files inside ANY registered project root, blocks secrets & outside files
    assert (await h_script.send("yt file", file=str(p_yt / "ok.txt")))["message_id"]
    assert (await h_script.send("web file", file=str(p_web / "index.ts")))["message_id"]
    for bad in (p_web / ".env", p_outside / "nope.txt"):
        with pytest.raises(HubError):
            await h_script.send("bad", file=str(bad))


async def test_hub_projects_mcp_tool_and_antigravity_config(tmp_path, monkeypatch):
    _, _, _, _, _, _, h_script, _ = await _setup_hub_team(tmp_path)
    server = build(h_script, Inbox(h_script.cfg.home))
    names = {t.name for t in await server.list_tools()}
    assert "hub_projects" in names

    proj = tmp_path / "my_repo"
    proj.mkdir()
    # Agents may only change folder mappings when the owner opted in explicitly.
    denied = await server.call_tool(
        "hub_projects", {"action": "upsert", "project_id": "repo1", "path": str(proj)})
    assert "only the human owner" in json.dumps([getattr(c, "text", "") for c in
                                                  getattr(denied, "content", denied[0] if isinstance(denied, tuple) else denied)])
    monkeypatch.setenv("AGENTHUB_AGENT_CAN_MAP_PROJECTS", "1")
    await server.call_tool(
        "hub_projects",
        {"action": "upsert", "project_id": "repo1", "path": str(proj), "aliases": ["repo1", "core"]},
    )
    res = await server.call_tool("hub_projects", {"action": "resolve", "sample_text": "check #repo1"})
    items = getattr(res, "content", res[0] if isinstance(res, tuple) else res)
    text = json.dumps([getattr(c, "text", "") for c in items])
    assert "repo1" in text

    # Onboarding config writer preserves existing MCP servers
    cfg_file = tmp_path / "mcp_config.json"
    cfg_file.write_text(json.dumps({"mcpServers": {"existing": {"command": "foo"}}}), encoding="utf-8")
    write_antigravity_mcp_config(cfg_file)
    written = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "existing" in written["mcpServers"] and "agenthub" in written["mcpServers"]
