"""Optional System-1 Task Router for AgentHub Local (`agenthub-local router`).

Uses Google DeepMind's `gemini-decision-1` (0 output tokens, ~200ms, native multimodal)
to route untagged human messages in `#agent-chat` to the best registered agent from `#agent-roster`.

Design invariants preserved:
  - ONLY the dedicated Router bot needs Discord's privileged Message Content Intent;
    all regular team member bots remain free of privileged intents.
  - The Router reads `#agent-roster` directly (single source of truth; no duplicate roster file).
  - The Router ONLY dispatches `<@bot_id>` mentions (or asks a clarifying question);
    it NEVER approves tasks, verifies knowledge, or overrides human maintainers.
  - Bot messages and messages that already @mention an agent are never re-routed (no loops).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from agenthub_local.config import Config
from agenthub_local.discord_rest import DiscordREST
from agenthub_local.hub import Hub, TEXT_TYPE, meta_embed

log = logging.getLogger("agenthub.router")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a"}
DOC_EXTS = {".pdf"}
MAX_ROUTER_ATTACHMENT = 10 * 1024 * 1024  # 10 MB


@dataclass
class RouteResult:
    decision: str  # ASSIGN | CLARIFY | IGNORE
    bot_id: str
    agent_name: str
    confidence: float
    is_task_prob: float
    urgency: float
    top_candidates: list[dict[str, Any]]
    elapsed_ms: int


class DecisionBackend(Protocol):
    """Protocol for pluggable System-1 decision backends (enables deterministic unit tests)."""

    def decide(
        self,
        prompt: str,
        questions: dict[str, dict[str, Any]],
        media: dict[str, Path | None] | None = None,
    ) -> dict[str, Any]: ...


class GeminiDecisionBackend:
    """Calls `models/gemini-decision-1` with multi-key round-robin & instant 429 failover.

    Key lookup order:
      1. Explicit `api_keys` argument / `AGENTHUB_GEMINI_KEYS` / `GEMINI_API_KEY` env vars.
      2. Optional local `gemini_decision_client.GeminiDecisionClient` pool if installed.
    """

    def __init__(self, api_keys: list[str] | None = None, model: str = "models/gemini-decision-1") -> None:
        self.model = model
        self._keys: list[str] = []
        self._cursor = 0
        self._cooldowns: dict[str, float] = {}
        self._external_gdec = None

        raw_keys = list(api_keys or [])
        for env_name in ("AGENTHUB_GEMINI_KEYS", "GEMINI_DECISION_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            val = os.environ.get(env_name, "").strip()
            if val:
                raw_keys.extend(k.strip() for k in val.replace(";", ",").split(",") if k.strip())
        seen: set[str] = set()
        for k in raw_keys:
            if k not in seen:
                seen.add(k)
                self._keys.append(k)

        if not self._keys:
            self._try_load_external_pool()

    def _try_load_external_pool(self) -> None:
        gdec_dir = Path.home() / ".gemini" / "config" / "skills" / "jev-vision"
        if gdec_dir.exists() and str(gdec_dir) not in sys.path:
            sys.path.insert(0, str(gdec_dir))
        try:
            from gemini_decision_client import GeminiDecisionClient  # type: ignore

            client = GeminiDecisionClient(fallback_to_jev=False)
            if client.pool.has_keys():
                self._external_gdec = client
        except Exception:
            self._external_gdec = None

    @staticmethod
    def _encode_media(path: Path, media_type: str) -> dict[str, Any]:
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return {
            "type": media_type,
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "mime_type": mime,
        }

    def decide(
        self,
        prompt: str,
        questions: dict[str, dict[str, Any]],
        media: dict[str, Path | None] | None = None,
    ) -> dict[str, Any]:
        media = media or {}
        if self._external_gdec is not None:
            return self._external_gdec.ask(
                prompt=prompt,
                questions=questions,
                image=media.get("image"),
                video=media.get("video"),
                audio=media.get("audio"),
                document=media.get("document"),
                include_confidence=True,
            )

        if not self._keys:
            raise RuntimeError(
                "No Gemini API keys configured for router. Set AGENTHUB_GEMINI_KEYS or GEMINI_API_KEY."
            )

        from google import genai  # lazy import so normal members don't need google-genai

        blocks: list[dict[str, Any]] = []
        for mtype in ("video", "audio", "document", "image"):
            p = media.get(mtype)
            if p is not None and p.exists():
                blocks.append(self._encode_media(p, mtype))
        blocks.append({"type": "text", "text": prompt})
        payload: Any = prompt if len(blocks) == 1 else blocks

        now = time.monotonic()
        n = len(self._keys)
        ordered = [self._keys[(self._cursor + i) % n] for i in range(n)]
        ready = [k for k in ordered if self._cooldowns.get(k, 0.0) <= now]
        cooling = [k for k in ordered if self._cooldowns.get(k, 0.0) > now]
        self._cursor = (self._cursor + 1) % n

        last_err: Exception | None = None
        for key in ready + cooling:
            try:
                client = genai.Client(api_key=key)
                resp = client.decisions.create(
                    model=self.model,
                    api_version="v1beta",
                    input=payload,
                    questions=questions,
                )
                out: dict[str, Any] = {}
                for qid, ans in (resp.answers or {}).items():
                    atype = getattr(ans, "type", None)
                    if atype == "noul":
                        out[qid] = {"noul": float(ans.noul), "confidence": float(ans.confidence)}
                    elif atype == "choice":
                        out[qid] = {
                            "choice": ans.choice,
                            "confidence": float(ans.confidence),
                            "probabilities": dict(ans.probabilities or {}),
                        }
                    elif atype == "score":
                        out[qid] = {"score": float(ans.score), "confidence": float(ans.confidence)}
                return out
            except Exception as exc:
                last_err = exc
                if "429" in str(exc) or "rate" in str(exc).lower():
                    self._cooldowns[key] = time.monotonic() + 62.0
                    continue
                raise
        raise RuntimeError(f"All Gemini Decision keys failed: {last_err}")


class TaskRouter:
    """Routes untagged human messages in `#agent-chat` using live `#agent-roster` + DecisionBackend."""

    def __init__(
        self,
        hub: Hub,
        backend: DecisionBackend,
        threshold: float = 0.70,
        router_bot_id: str = "",
    ) -> None:
        self.hub = hub
        self.backend = backend
        self.threshold = threshold
        self.router_bot_id = router_bot_id

    async def route_message(
        self,
        sender: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
        recent_context: list[dict[str, Any]] | None = None,
        local_media: dict[str, Path | None] | None = None,
    ) -> RouteResult:
        roster = [a for a in await self.hub.roster() if a.get("bot_id") and a["bot_id"] != self.router_bot_id]
        if not roster:
            return RouteResult("IGNORE", "", "", 0.0, 0.0, 1.0, [], 0)

        by_id = {str(a["bot_id"]): a for a in roster}
        roster_lines = [
            f"- {a['bot_id']} ({a.get('name', 'agent')}): role={a.get('role', 'generalist')} model={a.get('model', '?')}"
            for a in roster
        ]
        criteria: dict[str, str] = {
            str(a["bot_id"]): f"Assign to {a.get('name', 'agent')} — role: {a.get('role', 'generalist')}"
            for a in roster
        }
        criteria["NONE"] = "Not an actionable task for any AI agent (casual human banter or greeting)."
        criteria["AMBIGUOUS"] = "Actionable task, but genuinely unclear which agent should own it."

        ctx_lines = [
            f"  {m.get('author', 'user')}: {(m.get('content') or '')[:200]}"
            for m in (recent_context or [])[-5:]
        ]
        att_names = [a.get("filename", "") for a in (attachments or []) if a.get("filename")]
        prompt = (
            "You are the System-1 Task Router for a team Discord #agent-chat.\n\n"
            "REGISTERED AI AGENTS (#agent-roster):\n"
            + "\n".join(roster_lines)
            + "\n\nRECENT CONTEXT:\n"
            + ("\n".join(ctx_lines) if ctx_lines else "  (none)")
            + f"\n\nNEW HUMAN MESSAGE from '{sender}' (attachments: {', '.join(att_names) or 'none'}):\n"
            + f"\"\"\"\n{content[:2000]}\n\"\"\"\n\n"
            "Rules:\n"
            "- Casual human banter/greetings -> is_actionable_task=false, assignee=NONE.\n"
            "- Actionable work/question matching one agent's role -> choose that agent's bot_id."
        )

        questions = {
            "is_actionable_task": {
                "type": "noul",
                "instructions": "Is this message an actionable request or question for one of the team AI agents (NOT casual banter)?",
            },
            "assignee": {
                "type": "choice",
                "instructions": "Which registered agent bot_id is the best owner? Choose NONE if no AI action needed, or AMBIGUOUS if unclear.",
                "criteria": criteria,
            },
            "urgency": {
                "type": "score",
                "instructions": "Rate urgency from 1 (casual/FYI) to 5 (critical urgent blocker).",
                "criteria": [
                    "1: Casual / FYI",
                    "2: Low priority",
                    "3: Normal team task",
                    "4: High priority",
                    "5: Critical urgent blocker",
                ],
            },
        }

        cleanup: list[Path] = []
        media = dict(local_media or {})
        if not media and attachments:
            media, cleanup = await asyncio.to_thread(self._download_first_media, attachments)

        t0 = time.perf_counter()
        try:
            res = await asyncio.to_thread(self.backend.decide, prompt, questions, media)
        finally:
            for p in cleanup:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        is_task = float((res.get("is_actionable_task") or {}).get("noul", 0.0))
        a_info = res.get("assignee") or {}
        choice = str(a_info.get("choice", "NONE"))
        conf = float(a_info.get("confidence", 0.0))
        probs = a_info.get("probabilities") or {}
        raw_urg = float((res.get("urgency") or {}).get("score", 2.0))
        urgency = round(min(5.0, max(1.0, raw_urg + 1.0)), 2)

        ranked = sorted(
            [
                {"bot_id": bid, "name": by_id[bid].get("name", bid), "prob": round(float(p), 4)}
                for bid, p in probs.items()
                if bid in by_id
            ],
            key=lambda x: x["prob"],
            reverse=True,
        )

        if is_task < 0.50 or choice == "NONE":
            decision = "IGNORE"
        elif choice in by_id and conf >= self.threshold:
            decision = "ASSIGN"
        elif ranked and ranked[0]["prob"] >= 0.35:
            decision = "CLARIFY"
        else:
            decision = "IGNORE"

        agent_name = str(by_id[choice].get("name", choice)) if choice in by_id else ""
        return RouteResult(
            decision=decision,
            bot_id=choice if choice in by_id else "",
            agent_name=agent_name,
            confidence=round(conf, 4),
            is_task_prob=round(is_task, 4),
            urgency=urgency,
            top_candidates=ranked[:3],
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _download_first_media(attachments: list[dict[str, Any]]) -> tuple[dict[str, Path | None], list[Path]]:
        media: dict[str, Path | None] = {}
        cleanup: list[Path] = []
        for att in attachments:
            url = str(att.get("url") or "")
            fname = str(att.get("filename") or "").lower()
            size = int(att.get("size") or 0)
            if not url or (size and size > MAX_ROUTER_ATTACHMENT):
                continue
            ext = Path(fname).suffix.lower()
            slot = (
                "image" if ext in IMAGE_EXTS else
                "video" if ext in VIDEO_EXTS else
                "audio" if ext in AUDIO_EXTS else
                "document" if ext in DOC_EXTS else None
            )
            if not slot:
                continue
            try:
                r = httpx.get(url, timeout=15.0, follow_redirects=True)
                if r.status_code == 200 and len(r.content) <= MAX_ROUTER_ATTACHMENT:
                    tmp = Path(tempfile.gettempdir()) / f"ah_route_{int(time.time() * 1000)}{ext}"
                    tmp.write_bytes(r.content)
                    media[slot] = tmp
                    cleanup.append(tmp)
                    break
            except httpx.HTTPError:
                continue
        return media, cleanup

    async def dispatch_if_needed(
        self,
        message_id: str,
        sender: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> RouteResult:
        recent = await self.hub.history(limit=5)
        res = await self.route_message(sender=sender, content=content, attachments=attachments, recent_context=recent)
        if res.decision == "IGNORE":
            return res

        chan = await self.hub._chan(self.hub.cfg.chat_channel, TEXT_TYPE)
        if res.decision == "ASSIGN" and res.bot_id:
            pct = int(round(res.confidence * 100))
            text = (
                f"<@{res.bot_id}> 📋 **Router → {res.agent_name}** "
                f"*(match {pct}% · urgency {res.urgency:.1f}/5 · {res.elapsed_ms}ms)* — "
                f"task from **{sender}**"
            )
            meta = {"t": "route", "hop": 0, "routed_to": res.bot_id, "confidence": res.confidence}
            await self.hub.rest.send(
                chan["id"],
                text,
                embed=meta_embed(meta),
                mention_user_ids=[res.bot_id],
                reply_to=message_id,
            )
        elif res.decision == "CLARIFY" and res.top_candidates:
            # Use plain bold names (NOT <@bot_id>) so ambiguous questions never wake bots prematurely
            opts = " or ".join(
                f"**{c['name']}** ({int(round(c['prob'] * 100))}%)" for c in res.top_candidates[:2]
            )
            text = f"🤔 **Router:** Should this go to {opts}? Reply with an `@mention` to wake them."
            meta = {"t": "route_clarify", "hop": 0}
            await self.hub.rest.send(chan["id"], text, embed=meta_embed(meta), reply_to=message_id)
        return res


def run_router_daemon(cfg: Config, threshold: float = 0.70) -> None:
    """Run the dedicated Discord Gateway Router bot (`agenthub-local router`)."""
    import discord

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True  # Only the single optional Router bot needs this

    backend = GeminiDecisionBackend()

    class _RouterClient(discord.Client):
        def __init__(self) -> None:
            super().__init__(intents=intents)
            self.rest = DiscordREST(cfg.token, cfg.api_base)
            self.hub = Hub(cfg, self.rest)
            self.router = TaskRouter(self.hub, backend=backend, threshold=threshold)

        async def on_ready(self) -> None:
            if self.user:
                self.router.router_bot_id = str(self.user.id)
            log.info("router listening as %s in guild %s (channel #%s)", self.user, cfg.guild_id, cfg.chat_channel)

        async def on_message(self, m: discord.Message) -> None:
            if m.guild is None or str(m.guild.id) != cfg.guild_id:
                return
            if m.author.bot:
                return
            if getattr(m.channel, "name", "") != cfg.chat_channel:
                return
            # Never interfere if the human already @mentioned a bot directly
            if any(u.bot for u in m.mentions):
                return
            atts = [
                {"filename": a.filename, "url": a.url, "size": a.size, "content_type": a.content_type or ""}
                for a in m.attachments
            ]
            if not (m.content or "").strip() and not atts:
                return
            try:
                res = await self.router.dispatch_if_needed(
                    message_id=str(m.id),
                    sender=m.author.display_name,
                    content=m.content or "",
                    attachments=atts,
                )
                log.info("routed msg %s -> %s (%s %.0f%%)", m.id, res.decision, res.agent_name, res.confidence * 100)
            except Exception as exc:
                log.warning("router failed on msg %s: %s", m.id, exc)

        async def close(self) -> None:
            await self.rest.close()
            await super().close()

    _RouterClient().run(cfg.token, log_handler=None)
