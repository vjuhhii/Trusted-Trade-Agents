"""Agent registry: which token belongs to which agent, buyer and mandate.

The registry is a JSON file so the limits can be read and changed without touching code.
`mandates.json` in the repository holds the defaults; the copy in `data/` is the one in use,
which the kitchen view edits, so the repository stays clean. In production this is the buyer's
account configuration at the wholesaler.
"""

import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import Field, TypeAdapter

from aimo_agent.config import settings
from aimo_agent.models import AgentIdentity, Approver, Mandate


class AgentRecord(AgentIdentity):
    token: str
    # Personal key -> name of a person who may approve for this buyer. Never shown to the agent.
    approver_keys: dict[str, str] = Field(default_factory=dict)


_records = TypeAdapter(list[AgentRecord])
_PRIVATE = {"token", "approver_keys"}


def load_agents(path: Path) -> dict[str, AgentIdentity]:
    return {
        record.token: AgentIdentity(**record.model_dump(exclude=_PRIVATE))
        for record in _records.validate_json(path.read_bytes())
    }


_cache: tuple[float, dict[str, AgentIdentity]] | None = None


def get_agents() -> dict[str, AgentIdentity]:
    """The registry, re-read whenever the file changes.

    The REST server and a stdio MCP server are separate processes: a mandate edited through
    one must be seen by the other on its next call.
    """
    global _cache
    path = _working_copy()
    modified = path.stat().st_mtime
    if _cache is None or _cache[0] != modified:
        _cache = (modified, load_agents(path))
    return _cache[1]


def get_approvers() -> dict[str, Approver]:
    """The people who may decide for each buyer, by their personal key.

    A working copy made before approver keys existed takes them from the repository defaults.
    """
    records = _records.validate_json(_working_copy().read_bytes())
    defaults = {
        record.agent_id: record.approver_keys
        for record in _records.validate_json(settings.aimo_agents_defaults.read_bytes())
    }
    return {
        key: Approver(
            name=name,
            agent_id=record.agent_id,
            buyer_id=record.buyer_id,
            buyer_name=record.buyer_name,
        )
        for record in records
        for key, name in (record.approver_keys or defaults.get(record.agent_id, {})).items()
    }


def _working_copy() -> Path:
    """The registry file in use, created from the repository's defaults on first run."""
    path = settings.aimo_agents_file
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(settings.aimo_agents_defaults, path)
    return path


def update_mandate(agent_id: str, changes: dict[str, Any]) -> AgentIdentity | None:
    """Change fields of one agent's mandate in the registry file. None if the agent is unknown."""
    path = _working_copy()
    records = _records.validate_json(path.read_bytes())
    record = next((r for r in records if r.agent_id == agent_id), None)
    if record is None:
        return None
    record.mandate = Mandate.model_validate(record.mandate.model_dump() | changes)
    path.write_text(
        json.dumps([r.model_dump(mode="json") for r in records], indent=2) + "\n", encoding="utf-8"
    )
    return AgentIdentity(**record.model_dump(exclude=_PRIVATE))
