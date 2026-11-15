from aimo_agent.contract import MCP_FILE, OPENAPI_FILE, mcp_markdown, openapi_json


def test_the_published_contract_is_what_runs() -> None:
    # Regenerate with: uv run python -m aimo_agent.contract
    assert OPENAPI_FILE.read_text(encoding="utf-8") == openapi_json()
    assert MCP_FILE.read_text(encoding="utf-8") == mcp_markdown()
