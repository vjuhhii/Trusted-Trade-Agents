import pytest
from mcp import Client

from aimo_agent.config import settings
from aimo_agent.mcp_server import mcp
from aimo_agent.models import AgentIdentity
from aimo_agent.service import PurchasingService

pytestmark = pytest.mark.anyio


@pytest.fixture
def wired(
    service: PurchasingService, hotel: AgentIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aimo_agent.mcp_server.get_service", lambda: service)
    monkeypatch.setattr(
        "aimo_agent.mcp_server.get_agents", lambda: {settings.aimo_agent_token: hotel}
    )


async def test_agent_buys_through_mcp_tools(wired: None) -> None:
    async with Client(mcp) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert {"get_mandate", "search_products", "request_options", "place_order"} <= tools

        request = await client.call_tool(
            "request_options",
            {
                "query": "milk",
                "free_from": ["lactose"],
                "exclude_terms": ["sour"],
                "quantity": 300,
                "unit": "litres",
                "deliver_by": "2026-11-16",
                "buyer_words": "I need 300 litres of lactose-free milk by Monday.",
            },
        )
        body = request.structured_content
        assert body["options"][0]["policy"]["decision"] == "auto_approve"

        order = await client.call_tool(
            "place_order",
            {"request_id": body["id"], "option_id": "opt-1", "reason": "Cheapest on time."},
        )
        assert order.structured_content["status"] == "confirmed"


async def test_expected_failures_reach_the_model_as_readable_errors(wired: None) -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("get_order", {"order_id": "PO-9999"})
        assert result.is_error
        assert "not_found" in result.content[0].text
