"""Agent registry: which token belongs to which agent, buyer and mandate.

The registry is a JSON file (mandates.json) so the limits can be read and changed without
touching code. In production this is the buyer's account configuration at the wholesaler.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from aimo_agent.config import settings
from aimo_agent.models import AgentIdentity, Mandate


class AgentRecord(AgentIdentity):
    token: str


_records = TypeAdapter(list[AgentRecord])


def load_agents(path: Path) -> dict[str, AgentIdentity]:
    return {
        record.token: AgentIdentity(**record.model_dump(exclude={"token"}))
        for record in _records.validate_json(path.read_bytes())
    }


_cache: tuple[float, dict[str, AgentIdentity]] | None = None


def get_agents() -> dict[str, AgentIdentity]:
    """The registry, re-read whenever the file changes.

    The REST server and a stdio MCP server are separate processes: a mandate edited through
    one must be seen by the other on its next call.
    """
    global _cache
    modified = settings.aimo_agents_file.stat().st_mtime
    if _cache is None or _cache[0] != modified:
        _cache = (modified, load_agents(settings.aimo_agents_file))
    return _cache[1]


def update_mandate(agent_id: str, changes: dict[str, Any]) -> AgentIdentity | None:
    """Change fields of one agent's mandate in the registry file. None if the agent is unknown."""
    path = settings.aimo_agents_file
    records = _records.validate_json(path.read_bytes())
    record = next((r for r in records if r.agent_id == agent_id), None)
    if record is None:
        return None
    record.mandate = Mandate.model_validate(record.mandate.model_dump() | changes)
    path.write_text(
        json.dumps([r.model_dump(mode="json") for r in records], indent=2) + "\n", encoding="utf-8"
    )
    return AgentIdentity(**record.model_dump(exclude={"token"}))
