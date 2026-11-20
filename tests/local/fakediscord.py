"""In-memory Discord API (subset) for tests. One shared server, many bot/user tokens."""

from __future__ import annotations

import itertools
import json
import re
from urllib.parse import unquote

import httpx

_ids = itertools.count(1_000_000_000_000_000)


def sid() -> str:
    return str(next(_ids))


class FakeDiscord:
    def __init__(self) -> None:
        self.guild_id = sid()
        self.users: dict[str, dict] = {}          # token -> user
        self.members: dict[str, list[str]] = {}   # user_id -> role ids
        self.roles: list[dict] = [{"id": self.guild_id, "name": "@everyone"}]
        self.channels: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}  # channel -> messages (oldest first)
        self.reactions: dict[tuple[str, str], list[dict]] = {}  # (msg, emoji) -> users

    # ---- test helpers ----
    def add_user(self, token: str, name: str, bot: bool) -> dict:
        u = {"id": sid(), "username": name, "bot": bot}
        self.users[token] = u
        self.members[u["id"]] = []
        return u

    def give_role(self, user: dict, role_name: str) -> None:
        rid = next(r["id"] for r in self.roles if r["name"] == role_name)
        self.members[user["id"]].append(rid)

    def react_as(self, user: dict, channel_id: str, message_id: str, emoji: str) -> None:
        self.reactions.setdefault((message_id, emoji), []).append(user)
        msg = next(m for m in self.messages[channel_id] if m["id"] == message_id)
        rs = msg.setdefault("reactions", [])
        hit = next((r for r in rs if r["emoji"]["name"] == emoji), None)
        if hit:
            hit["count"] += 1
        else:
            rs.append({"emoji": {"name": emoji}, "count": 1})

    def can_view(self, user: dict, channel_id: str) -> bool:
        """Discord-like VIEW_CHANNEL check using permission overwrites (threads inherit parent)."""
        c = self.channels.get(channel_id)
        if c is None:
            return True
        if c.get("type") == 11 and c.get("parent_id"):
            return self.can_view(user, c["parent_id"])
        ows = c.get("permission_overwrites") or []
        view = 1 << 10
        allowed = True
        for ow in ows:
            if ow["id"] == self.guild_id and int(ow.get("deny", 0)) & view:
                allowed = False
        for ow in ows:
            if int(ow["type"]) == 0 and ow["id"] in self.members.get(user["id"], []) \
                    and int(ow.get("allow", 0)) & view:
                allowed = True
        for ow in ows:
            if int(ow["type"]) == 1 and ow["id"] == user["id"]:
                if int(ow.get("allow", 0)) & view:
                    allowed = True
                if int(ow.get("deny", 0)) & view:
                    allowed = False
        return allowed

    def transport(self, token: str) -> httpx.MockTransport:
        return httpx.MockTransport(lambda req: self._handle(token, req))

    # ---- routing ----
    def _msg(self, channel_id: str, author: dict, payload: dict) -> dict:
        m = {"id": sid(), "channel_id": channel_id, "author": author, "content": payload.get("content", ""),
             "embeds": payload.get("embeds", []), "mentions": [], "reactions": []}
        for uid in re.findall(r"<@!?(\d+)>", m["content"]):
            u = next((x for x in self.users.values() if x["id"] == uid), None)
            if u:
                m["mentions"].append(u)
        if payload.get("attachments"):
            m["attachments"] = payload["attachments"]
        self.messages.setdefault(channel_id, []).append(m)
        return m

    def _handle(self, token: str, req: httpx.Request) -> httpx.Response:
        me = self.users.get(req.headers["Authorization"].removeprefix("Bot "))
        if me is None:
            return httpx.Response(401, json={"message": "401: Unauthorized"})
        path = req.url.path.removeprefix("/api/v10")
        q = dict(req.url.params)
        body = None
        if req.content:
            ctype = req.headers.get("content-type", "")
            if ctype.startswith("application/json"):
                body = json.loads(req.content)
            elif "multipart" in ctype:
                raw = req.content.decode("latin-1")
                pj = re.search(r'name="payload_json"\r\n\r\n(.*?)\r\n--', raw, re.DOTALL)
                body = json.loads(pj.group(1)) if pj else {}
        m, P = req.method, path
        g = self.guild_id
        J = lambda x, s=200: httpx.Response(s, json=x)

        if m == "GET" and P == "/users/@me":
            return J(me)
        if P == f"/guilds/{g}/channels":
            if m == "GET":
                return J(list(self.channels.values()))
            c = {"id": sid(), "guild_id": g, "name": body["name"], "type": body["type"],
                 "available_tags": [{"id": sid(), "name": t["name"]} for t in body.get("available_tags", [])],
                 "permission_overwrites": [
                     {"id": str(o["id"]), "type": int(o["type"]), "allow": str(o.get("allow", "0")),
                      "deny": str(o.get("deny", "0"))} for o in body.get("permission_overwrites", [])]}
            self.channels[c["id"]] = c
            return J(c, 201)
        if P == f"/guilds/{g}/roles":
            if m == "GET":
                return J(self.roles)
            r = {"id": sid(), "name": body["name"]}
            self.roles.append(r)
            return J(r)
        mm = re.fullmatch(rf"/guilds/{g}/members/(\d+)", P)
        if mm:
            uid = mm.group(1)
            if uid not in self.members:
                return J({"message": "Unknown Member"}, 404)
            return J({"user": {"id": uid}, "roles": self.members[uid]})
        if m == "GET" and P == f"/guilds/{g}/threads/active":
            return J({"threads": [c for c in self.channels.values() if c["type"] == 11 and self.can_view(me, c["id"])]})
        mm = re.fullmatch(r"/channels/(\d+)/permissions/(\d+)", P)
        if mm:
            c = self.channels[mm.group(1)]
            ows = [o for o in c.get("permission_overwrites", []) if o["id"] != mm.group(2)]
            if m == "PUT":
                ows.append({"id": mm.group(2), "type": int(body["type"]), "allow": str(body["allow"]),
                            "deny": str(body.get("deny", "0"))})
            c["permission_overwrites"] = ows
            return httpx.Response(204)
        mm = re.match(r"/channels/(\d+)", P)
        if mm and not self.can_view(me, mm.group(1)):
            return J({"message": "Missing Access", "code": 50001}, 403)
        mm = re.fullmatch(r"/channels/(\d+)/threads/archived/public", P)
        if mm:
            return J({"threads": [], "has_more": False})
        mm = re.fullmatch(r"/channels/(\d+)/threads", P)
        if mm and m == "POST":
            forum = mm.group(1)
            tid = sid()
            t = {"id": tid, "type": 11, "parent_id": forum, "name": body["name"],
                 "applied_tags": body.get("applied_tags", []), "owner_id": me["id"]}
            self.channels[tid] = t
            starter = self._msg(tid, me, body["message"])
            starter["id"] = tid  # forum starter message id == thread id
            return J({**t, "message": starter}, 201)
        mm = re.fullmatch(r"/channels/(\d+)", P)
        if mm:
            c = self.channels.get(mm.group(1))
            if c is None:
                return J({"message": "Unknown Channel"}, 404)
            if m == "PATCH":
                c.update(body)
            return J(c)
        mm = re.fullmatch(r"/channels/(\d+)/messages", P)
        if mm:
            cid = mm.group(1)
            if m == "POST":
                return J(self._msg(cid, me, body or {}))
            msgs = sorted(self.messages.get(cid, []), key=lambda x: int(x["id"]), reverse=True)
            if "after" in q:
                msgs = [x for x in msgs if int(x["id"]) > int(q["after"])]
            if "before" in q:
                msgs = [x for x in msgs if int(x["id"]) < int(q["before"])]
            return J(msgs[: int(q.get("limit", 50))])
        mm = re.fullmatch(r"/channels/(\d+)/messages/(\d+)/reactions/([^/]+)(/@me)?", P)
        if mm:
            cid, mid, emoji = mm.group(1), mm.group(2), unquote(mm.group(3))
            if m == "PUT":
                self.react_as(me, cid, mid, emoji)
                return httpx.Response(204)
            return J(self.reactions.get((mid, emoji), []))
        mm = re.fullmatch(r"/channels/(\d+)/messages/(\d+)", P)
        if mm:
            msg = next((x for x in self.messages.get(mm.group(1), []) if x["id"] == mm.group(2)), None)
            return J(msg) if msg else J({"message": "Unknown Message"}, 404)
        return J({"message": f"fake: unhandled {m} {P}"}, 500)
