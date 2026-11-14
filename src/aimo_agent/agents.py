"""Agent registry: which token belongs to which agent, buyer and mandate.

The registry is a JSON file (mandates.json) so the limits can be read and changed without
touching code. In production this is the buyer's account configuration at the wholesaler.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import TypeAdapter

from aimo_agent.config import settings
from aimo_agent.models import AgentIdentity


class AgentRecord(AgentIdentity):
    token: str


def load_agents(path: Path) -> dict[str, AgentIdentity]:
    records = TypeAdapter(list[AgentRecord]).validate_json(path.read_bytes())
    return {r.token: AgentIdentity(**r.model_dump(exclude={"token"})) for r in records}


@lru_cache
def get_agents() -> dict[str, AgentIdentity]:
    return load_agents(settings.aimo_agents_file)
