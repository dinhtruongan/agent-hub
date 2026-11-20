"""Interactive onboarding wizard (`agenthub-local init`) for Antigravity, Cursor & Claude Code."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from agenthub_local.projects import ProjectRegistry


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


def _mcp_command_spec() -> dict[str, object]:
    exe = shutil.which("agenthub-local")
    if exe:
        return {"command": exe, "args": ["mcp"]}
    return {"command": sys.executable, "args": ["-m", "agenthub_local.cli", "mcp"]}


def write_antigravity_mcp_config(config_path: Path | None = None) -> Path:
    path = config_path or (Path.home() / ".gemini" / "config" / "mcp_config.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("mcpServers", {})
    data["mcpServers"]["agenthub"] = _mcp_command_spec()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run_init_wizard(home: Path | None = None) -> None:
    home = home or (Path.home() / ".agenthub")
    home.mkdir(parents=True, exist_ok=True)
    env_path = home / "agenthub.env"

    print("=" * 60)
    print("  AgentHub Local — Interactive Onboarding Wizard")
    print("=" * 60)

    token = _ask("AGENTHUB_DISCORD_TOKEN (your personal bot token)")
    guild_id = _ask("AGENTHUB_GUILD_ID (Discord Server ID)")
    owner_id = _ask("AGENTHUB_OWNER_ID (your Discord User ID)")
    role = _ask("AGENTHUB_AGENT_ROLE (e.g. developer, reviewer, script, video)", "generalist")
    upload_root = _ask("Default project folder path (AGENTHUB_UPLOAD_ROOT, optional)", "")

    lines = [
        f"AGENTHUB_DISCORD_TOKEN={token}",
        f"AGENTHUB_GUILD_ID={guild_id}",
        f"AGENTHUB_OWNER_ID={owner_id}",
        f"AGENTHUB_AGENT_ROLE={role}",
    ]
    if upload_root:
        lines.append(f"AGENTHUB_UPLOAD_ROOT={upload_root}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nSaved {env_path}")

    if upload_root:
        reg = ProjectRegistry(home, fallback_root=Path(upload_root))
        pid = _ask("Short project ID for local projects.json (e.g. main, web, youtube)", "main")
        reg.upsert(pid, upload_root)
        print(f"Saved local project '{pid}' -> {reg.path}")

    if _ask("Auto-register 'agenthub' in Antigravity mcp_config.json? (y/n)", "y").lower().startswith("y"):
        mcp_path = write_antigravity_mcp_config()
        print(f"Updated {mcp_path}")

    spec = _mcp_command_spec()
    print("\nFor Claude Code:  claude mcp add agenthub -- agenthub-local mcp")
    print("For Cursor / other IDEs:", json.dumps({"mcpServers": {"agenthub": spec}}, indent=2))
    print("\nNext steps:")
    print("  1. Run `agenthub-local doctor` to verify Discord access.")
    print("  2. Restart your IDE and run `agenthub-local daemon` (or scripts/windows/start_daemon.bat).")
