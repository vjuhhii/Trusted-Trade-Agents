"""Empty the gateway's store: requests, orders and the audit log. Safe while the server runs.

Run: uv run python -m aimo_agent.reset              # orders, requests and the audit log
     uv run python -m aimo_agent.reset --mandates  # also put the agents' rules back to defaults
"""

import shutil
import sys

from aimo_agent.config import settings
from aimo_agent.store import Store

if __name__ == "__main__":
    Store(settings.aimo_db_path).clear()
    print(f"Cleared {settings.aimo_db_path}")
    if "--mandates" in sys.argv[1:]:
        settings.aimo_agents_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(settings.aimo_agents_defaults, settings.aimo_agents_file)
        print(f"Restored {settings.aimo_agents_file} from {settings.aimo_agents_defaults}")
