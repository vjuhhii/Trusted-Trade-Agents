"""The published contract: the REST API as OpenAPI 3.1 and the MCP tools as a reference page.

Both are generated from the code, so what is published is what runs. A test fails when the files
in docs/ no longer match.

    uv run python -m aimo_agent.contract    # rewrite docs/openapi.json and docs/mcp-tools.md
"""

import asyncio
import json
from typing import Any

from mcp import Client

from aimo_agent.app import app
from aimo_agent.config import PROJECT_ROOT
from aimo_agent.mcp_server import INSTRUCTIONS, mcp

DOCS = PROJECT_ROOT / "docs"
OPENAPI_FILE = DOCS / "openapi.json"
MCP_FILE = DOCS / "mcp-tools.md"


def openapi_json() -> str:
    return json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"


def _type(schema: dict[str, Any]) -> str:
    """A JSON Schema type in a few characters: string, number, Need[], one of L | KG."""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "enum" in schema:
        return " \\| ".join(str(value) for value in schema["enum"])
    if "const" in schema:
        return str(schema["const"])
    if "anyOf" in schema:
        return " or ".join(_type(option) for option in schema["anyOf"])
    kind = schema.get("type", "any")
    if kind == "array":
        return f"{_type(schema.get('items', {}))}[]"
    if kind == "string" and "format" in schema:
        return f"string ({schema['format']})"
    return str(kind)


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _fields(schema: dict[str, Any]) -> list[str]:
    required = set(schema.get("required", []))
    rows = ["| Field | Type | Required | Description |", "|---|---|---|---|"]
    for name, field in schema.get("properties", {}).items():
        rows.append(
            f"| `{name}` | {_cell(_type(field))} | {'yes' if name in required else ''} "
            f"| {_cell(field.get('description', ''))} |"
        )
    return rows


async def _tools() -> list[Any]:
    async with Client(mcp) as client:
        return list((await client.list_tools()).tools)


def mcp_markdown() -> str:
    lines = [
        "# MCP tools",
        "",
        "The agent-facing interface of the gateway, served over Streamable HTTP at `/mcp` (and "
        "stdio). Generated from the code by `uv run python -m aimo_agent.contract`; a test keeps "
        "it in step. The REST equivalent is [openapi.json](openapi.json).",
        "",
        "## Instructions every agent receives",
        "",
        "```text",
        INSTRUCTIONS.strip(),
        "```",
    ]
    for tool in asyncio.run(_tools()):
        hints = tool.annotations
        kind = "reads only" if hints and hints.read_only_hint else "records a request or an order"
        lines += ["", f"## `{tool.name}`", "", f"**{tool.title or tool.name}**, {kind}.", ""]
        paragraphs = [" ".join(p.split()) for p in (tool.description or "").split("\n\n")]
        lines += "\n\n".join(paragraphs).split("\n")
        schema = tool.input_schema
        if schema.get("properties"):
            lines += ["", *_fields(schema)]
        for name, definition in schema.get("$defs", {}).items():
            if definition.get("properties"):
                lines += ["", f"`{name}`:", "", *_fields(definition)]
    return "\n".join(lines) + "\n"


def write() -> None:
    DOCS.mkdir(exist_ok=True)
    OPENAPI_FILE.write_text(openapi_json(), encoding="utf-8")
    MCP_FILE.write_text(mcp_markdown(), encoding="utf-8")


if __name__ == "__main__":
    write()
    print(f"Wrote {OPENAPI_FILE} and {MCP_FILE}")
