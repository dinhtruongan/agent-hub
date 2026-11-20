"""Minimal Discord REST client (API v10) with rate-limit handling.

Every request is authenticated with this member's own bot token, so Discord itself enforces who
is speaking. Mentions are never parsed implicitly (allowed_mentions), so an agent cannot ping
@everyone or roles.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

MAX_UPLOAD = 8 * 1024 * 1024


class DiscordError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Discord API {status}: {detail[:300]}")
        self.status = status


class DiscordREST:
    def __init__(self, token: str, base: str = "https://discord.com/api/v10",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base, transport=transport, timeout=20,
            headers={"Authorization": f"Bot {token}",
                     "User-Agent": "DiscordBot (https://github.com/dinhtruongan/agent-hub, 0.2)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def req(self, method: str, path: str, **kw: Any) -> Any:
        for _ in range(5):
            r = await self._client.request(method, path, **kw)
            if r.status_code == 429:
                await asyncio.sleep(float(r.json().get("retry_after", 1.0)))
                continue
            if r.status_code >= 400:
                raise DiscordError(r.status_code, r.text)
            return r.json() if r.content else None
        raise DiscordError(429, "rate limited repeatedly")

    # ---- identity / guild ----
    async def me(self) -> dict:
        return await self.req("GET", "/users/@me")

    async def guild_channels(self, guild_id: str) -> list[dict]:
        return await self.req("GET", f"/guilds/{guild_id}/channels")

    async def guild_roles(self, guild_id: str) -> list[dict]:
        return await self.req("GET", f"/guilds/{guild_id}/roles")

    async def member(self, guild_id: str, user_id: str) -> dict | None:
        try:
            return await self.req("GET", f"/guilds/{guild_id}/members/{user_id}")
        except DiscordError as e:
            if e.status == 404:
                return None
            raise

    async def create_channel(self, guild_id: str, body: dict) -> dict:
        return await self.req("POST", f"/guilds/{guild_id}/channels", json=body)

    async def set_permission(self, channel_id: str, target_id: str, allow: int, deny: int = 0,
                             kind: int = 1) -> None:
        """Channel permission overwrite; kind 0 = role, 1 = member (user or bot)."""
        await self.req("PUT", f"/channels/{channel_id}/permissions/{target_id}",
                       json={"allow": str(allow), "deny": str(deny), "type": kind})

    async def delete_permission(self, channel_id: str, target_id: str) -> None:
        await self.req("DELETE", f"/channels/{channel_id}/permissions/{target_id}")

    async def create_role(self, guild_id: str, name: str) -> dict:
        return await self.req("POST", f"/guilds/{guild_id}/roles", json={"name": name})

    # ---- messages ----
    async def send(self, channel_id: str, content: str, *, embed: dict | None = None,
                   mention_user_ids: list[str] | None = None, file: Path | None = None,
                   reply_to: str | None = None) -> dict:
        payload: dict[str, Any] = {
            "content": content[:2000],
            "allowed_mentions": {"parse": [], "users": (mention_user_ids or [])[:100]},
        }
        if embed:
            payload["embeds"] = [embed]
        if reply_to:
            payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        if file is None:
            return await self.req("POST", f"/channels/{channel_id}/messages", json=payload)
        payload["attachments"] = [{"id": 0, "filename": file.name}]
        files = {"files[0]": (file.name, file.read_bytes())}
        return await self.req("POST", f"/channels/{channel_id}/messages",
                              data={"payload_json": json.dumps(payload)}, files=files)

    async def messages(self, channel_id: str, limit: int = 50, after: str | None = None,
                       before: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": max(1, min(100, limit))}
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        return await self.req("GET", f"/channels/{channel_id}/messages", params=params)

    async def message(self, channel_id: str, message_id: str) -> dict:
        return await self.req("GET", f"/channels/{channel_id}/messages/{message_id}")

    async def reactions(self, channel_id: str, message_id: str, emoji: str) -> list[dict]:
        from urllib.parse import quote

        return await self.req("GET", f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}",
                              params={"limit": 100})

    async def react(self, channel_id: str, message_id: str, emoji: str) -> None:
        from urllib.parse import quote

        await self.req("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me")

    # ---- forums / threads ----
    async def channel(self, channel_id: str) -> dict:
        return await self.req("GET", f"/channels/{channel_id}")

    async def forum_post(self, forum_id: str, name: str, content: str, tag_ids: list[str],
                         embed: dict | None = None) -> dict:
        msg: dict[str, Any] = {"content": content[:2000], "allowed_mentions": {"parse": []}}
        if embed:
            msg["embeds"] = [embed]
        return await self.req("POST", f"/channels/{forum_id}/threads",
                              json={"name": name[:100], "message": msg, "applied_tags": tag_ids[:5]})

    async def edit_thread(self, thread_id: str, **fields: Any) -> dict:
        return await self.req("PATCH", f"/channels/{thread_id}", json=fields)

    async def active_threads(self, guild_id: str) -> list[dict]:
        return (await self.req("GET", f"/guilds/{guild_id}/threads/active")).get("threads", [])

    async def archived_threads(self, channel_id: str) -> list[dict]:
        out: list[dict] = []
        before = None
        for _ in range(10):
            params = {"limit": 100}
            if before:
                params["before"] = before
            page = await self.req("GET", f"/channels/{channel_id}/threads/archived/public", params=params)
            out += page.get("threads", [])
            if not page.get("has_more") or not page.get("threads"):
                break
            before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]
        return out
