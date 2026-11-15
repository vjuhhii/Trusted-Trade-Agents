from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment variables and .env."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # Upstream: the Valio Aimo mock API.
    aimo_api_base_url: str = ""
    aimo_api_key: str = ""

    # This service.
    aimo_db_path: Path = DATA_DIR / "purchasing.sqlite3"
    # Agents and their mandates. The repository file holds the defaults; the working copy,
    # which the kitchen view edits, lives with the other runtime data.
    aimo_agents_defaults: Path = PROJECT_ROOT / "mandates.json"
    aimo_agents_file: Path = DATA_DIR / "mandates.json"
    # Ed25519 key the gateway signs offers with. Created on first start.
    aimo_signing_key_file: Path = DATA_DIR / "signing_key.pem"
    # Identity the stdio MCP server acts as. An HTTP client sends its own bearer token instead.
    aimo_agent_token: str = "demo-nordic-hotels-agent"


settings = Settings()
