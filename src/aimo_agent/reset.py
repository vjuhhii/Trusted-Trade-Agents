"""Empty the gateway's store: requests, orders and the audit log. Safe while the server runs.

Run: uv run python -m aimo_agent.reset
"""

from aimo_agent.config import settings
from aimo_agent.store import Store

if __name__ == "__main__":
    Store(settings.aimo_db_path).clear()
    print(f"Cleared {settings.aimo_db_path}")
