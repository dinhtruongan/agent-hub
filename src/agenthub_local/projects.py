"""Per-machine local project workspace registry (`~/.agenthub/projects.json`).

Allows one team member to work across multiple local project directories and map Discord
channels, `#project` tags, or aliases to the exact path on their machine without pushing
machine-specific paths to Git. Also integrates with Hub's strict upload containment guard.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class ProjectRegistry:
    def __init__(self, home: Path, fallback_root: Path | None = None) -> None:
        self.home = home
        self.path = home / "projects.json"
        self.fallback_root = fallback_root.resolve() if fallback_root else None

    def load(self) -> dict[str, Any]:
        default_ws = str(self.fallback_root) if self.fallback_root else ""
        if not self.path.exists():
            return {"default_workspace": default_ws, "projects": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("default_workspace", default_ws)
                data.setdefault("projects", [])
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"default_workspace": default_ws, "projects": []}

    def save(self, cfg: dict[str, Any]) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def set_default(self, workspace_path: str) -> dict[str, Any]:
        resolved = str(Path(workspace_path).expanduser().resolve())
        cfg = self.load()
        cfg["default_workspace"] = resolved
        self.save(cfg)
        return {"ok": True, "default_workspace": resolved}

    def upsert(
        self,
        project_id: str,
        workspace_path: str,
        name: str = "",
        channel_ids: list[str] | None = None,
        aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        pid = project_id.strip().lower()
        if not pid or not workspace_path.strip():
            raise ValueError("Both project_id and workspace_path are required")
        resolved = str(Path(workspace_path).expanduser().resolve())
        al = [a.strip().lower() for a in (aliases or []) if a.strip()]
        if pid not in al:
            al.insert(0, pid)
        ch = [str(c).strip() for c in (channel_ids or []) if str(c).strip()]
        entry = {
            "id": pid,
            "name": name.strip() or Path(resolved).name or pid,
            "path": resolved,
            "channel_ids": ch,
            "aliases": al,
        }
        cfg = self.load()
        projects: list[dict[str, Any]] = cfg.get("projects") or []
        updated = False
        for i, p in enumerate(projects):
            if str(p.get("id", "")).lower() == pid:
                projects[i] = entry
                updated = True
                break
        if not updated:
            projects.append(entry)
        cfg["projects"] = projects
        if not cfg.get("default_workspace"):
            cfg["default_workspace"] = resolved
        self.save(cfg)
        return {"ok": True, "action": "updated" if updated else "added", "project": entry}

    def get(self, project_id: str) -> dict[str, Any] | None:
        """The local mapping for one project, or None if this machine has not opted in."""
        pid = project_id.strip().lower()
        for p in self.load().get("projects") or []:
            if str(p.get("id", "")).lower() == pid and str(p.get("path", "")).strip():
                return p
        return None

    def remove(self, project_id: str) -> dict[str, Any]:
        pid = project_id.strip().lower()
        cfg = self.load()
        before = len(cfg.get("projects") or [])
        cfg["projects"] = [p for p in (cfg.get("projects") or []) if str(p.get("id", "")).lower() != pid]
        self.save(cfg)
        return {"ok": len(cfg["projects"]) < before, "removed": pid}

    def allowed_roots(self) -> list[Path]:
        """All local roots permitted for file uploads (upload_root + registered project paths)."""
        roots: list[Path] = []
        seen: set[Path] = set()
        if self.fallback_root:
            roots.append(self.fallback_root)
            seen.add(self.fallback_root)
        cfg = self.load()
        for raw in [cfg.get("default_workspace", "")] + [p.get("path", "") for p in cfg.get("projects") or []]:
            if not raw or not str(raw).strip():
                continue
            try:
                rp = Path(str(raw)).expanduser().resolve()
                if rp not in seen:
                    roots.append(rp)
                    seen.add(rp)
            except OSError:
                continue
        return roots

    def resolve(self, channel_id: str = "", text: str = "") -> dict[str, str]:
        """Resolve the target local workspace for an incoming Discord message.

        Priority:
          1. Explicit `project:<id>` / `duan:<id>` / `#<id>` tag in message text.
          2. Keyword / alias match in message text (longest alias wins).
          3. Channel ID mapped in `projects.json`.
          4. `default_workspace` fallback.
        """
        cfg = self.load()
        projects = cfg.get("projects") or []
        text_low = (text or "").lower()
        cid = str(channel_id or "").strip()

        explicit = re.search(r"(?:project:|duan:|#)\s*([a-z0-9_-]+)", text_low)
        if explicit:
            key = explicit.group(1)
            for p in projects:
                pid = str(p.get("id", "")).lower()
                aliases = [str(a).lower() for a in (p.get("aliases") or [])]
                if key == pid or key in aliases:
                    return {
                        "project_id": str(p.get("id", "")),
                        "project_name": str(p.get("name", pid)),
                        "workspace_path": str(p.get("path", "")),
                        "matched_by": f"explicit:{key}",
                    }

        best: tuple[dict[str, Any], str] | None = None
        best_len = 0
        for p in projects:
            pid = str(p.get("id", "")).lower()
            candidates = [pid] + [str(a).lower() for a in (p.get("aliases") or [])]
            for kw in candidates:
                if len(kw) < 2:
                    continue
                if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text_low) and len(kw) > best_len:
                    best_len = len(kw)
                    best = (p, kw)
        if best:
            p, kw = best
            return {
                "project_id": str(p.get("id", "")),
                "project_name": str(p.get("name", p.get("id", ""))),
                "workspace_path": str(p.get("path", "")),
                "matched_by": f"alias:{kw}",
            }

        if cid:
            for p in projects:
                if cid in [str(x).strip() for x in (p.get("channel_ids") or [])]:
                    return {
                        "project_id": str(p.get("id", "")),
                        "project_name": str(p.get("name", p.get("id", ""))),
                        "workspace_path": str(p.get("path", "")),
                        "matched_by": f"channel:{cid}",
                    }

        default_ws = str(cfg.get("default_workspace") or "").strip()
        return {
            "project_id": "default" if default_ws else "",
            "project_name": Path(default_ws).name if default_ws else "",
            "workspace_path": default_ws,
            "matched_by": "default" if default_ws else "none",
        }
